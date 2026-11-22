"""Settings, read from environment variables prefixed STIFLYT2_ or a .env file."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STIFLYT2_", env_file=".env", extra="ignore")

    # libpq connection strings. The importer connects as stiflyt2_import, which
    # has no rights in core or history.
    import_dsn: str = "dbname=stiflyt2 user=stiflyt2_import"
    # The API reads source_index as stiflyt2_api, which cannot see source_raw.
    api_dsn: str = "dbname=stiflyt2 user=stiflyt2_api"
    # Who is recorded as the author of changes to owned data. While this is
    # empty the API accepts no changes at all.
    editor: str = ""
    # Sign-in with Google, as v1 has it. With a client id set, nobody gets in without signing
    # in and being listed in access.person, and `editor` above is not used. Without one, the
    # service answers only on localhost, as the one editor named above.
    google_client_id: str = ""
    google_client_secret: str = ""
    session_secret: str = ""                # signs the session cookie; changing it signs everybody out
    public_url: str = ""                    # e.g. https://stiflyt2.example.org: where Google sends people back
    # Whose routes the page shows to begin with: the areas list and the report can be narrowed to
    # what one maintainer looks after, and this is the one chosen until a person chooses otherwise.
    home_maintainer: str = "DNT | DNT Oslo og omegn"
    data_dir: Path = Path("var/raw")
    # Photos, each kept once by the digest of its file, with a smaller copy to show and a thumbnail. Not in the
    # nightly copies off the server: they are to go to object storage (docs/TODO.md).
    photos_dir: Path = Path("var/photos")
    user_agent: str = "stiflyt2-importer/0.1 (DNT route maintenance)"
    # A delivery in which more than this share of a layer's features changed is refused.
    max_churn: float = 0.5
    # Original files are kept for this many of the newest snapshots per source
    # and scope; the database keeps every version regardless. 0 keeps all files.
    keep_artifacts: int = 14


settings = Settings()
