"""Settings, read from environment variables prefixed STIFLYT2_ or a .env file."""
from pathlib import Path

from pydantic import AliasChoices, Field
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
    # GPX tracks as they came, each kept once by the digest of its file. Not in the nightly copies either; what the
    # tracks are (their points, heights and times) is, in the database.
    tracks_dir: Path = Path("var/tracks")
    user_agent: str = "stiflyt2-importer/0.1 (DNT route maintenance)"
    # A delivery in which more than this share of a layer's features changed is refused.
    max_churn: float = 0.5
    # Original files are kept for this many of the newest snapshots per source
    # and scope; the database keeps every version regardless. 0 keeps all files.
    keep_artifacts: int = 14
    # Where what the page warns of (a night that went wrong) is sent, after the nightly jobs: by e-mail through
    # an SMTP server (port 465: TLS from the start; otherwise STARTTLS), and/or as a POST of plain text to a web
    # address (a push service such as an ntfy topic). Neither set: nothing is sent, the page still says it.
    alert_to: str = ""
    alert_from: str = ""
    alert_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Kartverket's Matrikkel API, for who owns a property (the feature `landowners`): an account granted DNT on
    # application. The names v1 uses are read too.
    matrikkel_username: str = Field("", validation_alias=AliasChoices("STIFLYT2_MATRIKKEL_USERNAME", "MATRIKKEL_USERNAME"))
    matrikkel_password: str = Field("", validation_alias=AliasChoices("STIFLYT2_MATRIKKEL_PASSWORD", "MATRIKKEL_PASSWORD"))
    matrikkel_url: str = "https://www.matrikkel.no/matrikkelapi/wsapi/v1"


settings = Settings()
