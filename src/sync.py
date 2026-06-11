"""
spotinew — Ajoute automatiquement les nouvelles sorties des artistes suivis
sur Spotify dans une playlist dédiée, à partir d'une date donnée (START_DATE).

Conçu pour tourner sans interaction (GitHub Actions) grâce à un refresh token.

Logique :
  1. Récupère les artistes suivis (scope user-follow-read).
  2. Pour chaque artiste, liste ses albums/singles parus >= START_DATE.
  3. Récupère les pistes de ces sorties.
  4. Ajoute à la playlist celles qui n'y sont pas déjà (dédoublonnage robuste).

Le script est « sans état » : à chaque exécution il compare les nouveautés au
contenu actuel de la playlist. Pas de fichier d'état à maintenir.
"""

from __future__ import annotations

import os
import sys
from datetime import date

import spotipy
from spotipy.oauth2 import SpotifyOAuth

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv est optionnel (absent en CI, vars déjà injectées)
    pass

SCOPES = "user-follow-read playlist-modify-private playlist-modify-public"


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        sys.exit(f"[ERREUR] Variable d'environnement manquante : {name}")
    return val


def get_client():
    """Construit un client Spotify authentifié à partir du refresh token."""
    auth = SpotifyOAuth(
        client_id=env("SPOTIFY_CLIENT_ID", required=True),
        client_secret=env("SPOTIFY_CLIENT_SECRET", required=True),
        redirect_uri=env("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope=SCOPES,
        open_browser=False,
        cache_handler=spotipy.cache_handler.MemoryCacheHandler(),
    )
    token_info = auth.refresh_access_token(env("SPOTIFY_REFRESH_TOKEN", required=True))
    return spotipy.Spotify(
        auth=token_info["access_token"],
        requests_timeout=30,
        retries=5,
        status_retries=5,
        backoff_factor=0.5,
    )


def parse_release_date(release_date, precision):
    """Convertit une release_date Spotify (année / mois / jour) en objet date."""
    parts = (release_date or "").split("-")
    try:
        year = int(parts[0])
    except (ValueError, IndexError):
        return None
    month = int(parts[1]) if len(parts) > 1 and precision != "year" else 1
    day = int(parts[2]) if len(parts) > 2 and precision == "day" else 1
    try:
        return date(year, month, day)
    except ValueError:
        return date(year, 1, 1)


def get_followed_artists(sp):
    """Récupère tous les artistes suivis (pagination par curseur)."""
    artists = []
    after = None
    while True:
        page = sp.current_user_followed_artists(limit=50, after=after)["artists"]
        items = page.get("items", [])
        artists.extend(items)
        after = (page.get("cursors") or {}).get("after")
        if not after or not items:
            break
    return artists


def get_recent_albums(sp, artist_id, floor, market):
    """Albums/singles de l'artiste parus à partir de `floor`.

    Dédoublonne par nom (en minuscules) pour éviter les versions multi-marchés.
    """
    albums = {}
    offset = 0
    while True:
        page = sp.artist_albums(
            artist_id,
            include_groups="album,single",
            country=market,
            limit=50,
            offset=offset,
        )
        items = page.get("items", [])
        for alb in items:
            rd = parse_release_date(alb.get("release_date"), alb.get("release_date_precision"))
            if not rd or rd < floor:
                continue
            key = alb["name"].strip().lower()
            if key not in albums:
                albums[key] = (rd, alb["name"], alb["id"])
        if page.get("next"):
            offset += len(items)
        else:
            break
    return list(albums.values())


def get_album_track_uris(sp, album_id):
    """Liste les pistes d'un album : (id, uri, nom)."""
    tracks = []
    results = sp.album_tracks(album_id, limit=50)
    while results:
        for t in results.get("items", []):
            if t and t.get("id"):
                tracks.append((t["id"], t["uri"], t["name"]))
        results = sp.next(results) if results.get("next") else None
    return tracks


def get_playlist_track_ids(sp, playlist_id):
    """Ensemble des IDs de pistes déjà présentes dans la playlist."""
    ids = set()
    results = sp.playlist_items(
        playlist_id, fields="items(track(id)),next", limit=100, additional_types=("track",)
    )
    while results:
        for it in results.get("items", []):
            t = it.get("track")
            if t and t.get("id"):
                ids.add(t["id"])
        results = sp.next(results) if results.get("next") else None
    return ids


def find_or_create_playlist(sp, me):
    """Renvoie l'ID de la playlist cible (par ID explicite, sinon par nom, sinon créée)."""
    playlist_id = env("SPOTIFY_PLAYLIST_ID")
    if playlist_id:
        return playlist_id

    name = env("SPOTIFY_PLAYLIST_NAME", "Nouveautés abonnements")
    user_id = me["id"]
    offset = 0
    while True:
        page = sp.current_user_playlists(limit=50, offset=offset)
        for pl in page["items"]:
            if pl and pl["name"] == name and pl["owner"]["id"] == user_id:
                return pl["id"]
        if page.get("next"):
            offset += len(page["items"])
        else:
            break

    print(f"[INFO] Playlist « {name} » introuvable — création…")
    pl = sp.user_playlist_create(
        user_id,
        name,
        public=False,
        description="Nouveautés des artistes suivis — alimentée automatiquement par spotinew.",
    )
    return pl["id"]


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def main():
    floor = date.fromisoformat(env("START_DATE", required=True))
    print(f"[INFO] Date plancher : {floor.isoformat()}")

    sp = get_client()
    me = sp.me()
    market = me.get("country")
    playlist_id = find_or_create_playlist(sp, me)
    print(f"[INFO] Playlist cible : {playlist_id}")

    existing = get_playlist_track_ids(sp, playlist_id)
    print(f"[INFO] {len(existing)} titre(s) déjà dans la playlist.")

    artists = get_followed_artists(sp)
    print(f"[INFO] {len(artists)} artiste(s) suivi(s).")

    seen = set(existing)
    new_tracks = []  # (release_date, uri)
    for i, artist in enumerate(artists, 1):
        albums = get_recent_albums(sp, artist["id"], floor, market)
        added_here = 0
        for _rd, _name, album_id in albums:
            for tid, uri, _tname in get_album_track_uris(sp, album_id):
                if tid not in seen:
                    seen.add(tid)
                    new_tracks.append((_rd, uri))
                    added_here += 1
        if albums:
            print(f"  [{i}/{len(artists)}] {artist['name']} — "
                  f"{len(albums)} sortie(s), {added_here} nouveau(x) titre(s)")

    # Ordre chronologique : les plus anciennes nouveautés en premier.
    new_tracks.sort(key=lambda x: x[0])
    uris = [uri for _, uri in new_tracks]

    if not uris:
        print("[OK] Aucune nouveauté à ajouter. Playlist déjà à jour.")
        return

    for batch in chunked(uris, 100):
        sp.playlist_add_items(playlist_id, batch)

    print(f"[OK] {len(uris)} titre(s) ajouté(s) à la playlist.")


if __name__ == "__main__":
    main()
