from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    secret_key: str = "dev-secret-change-me"
    encryption_key: str
    database_url: str = "sqlite:///./data/app.db"
    upload_dir: str = "./app/uploads"
    admin_username: str = "admin"
    admin_password: str
    max_posts_per_day: int = 50

settings = Settings()
