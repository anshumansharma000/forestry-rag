from supabase import Client

from services.storage import supabase_client

USER_PUBLIC_COLUMNS = "id,email,full_name,role,is_active,must_change_password,last_login_at,metadata,created_at,updated_at"


class AuthRepository:
    def __init__(self, client: Client | None = None):
        self.client = client or supabase_client()

    def get_user_for_auth(self, email: str) -> dict | None:
        result = (
            self.client.table("app_users")
            .select(f"{USER_PUBLIC_COLUMNS},password_hash")
            .eq("email", email.strip().lower())
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_user_by_id(self, user_id: str, columns: str = "id,email,full_name,role,is_active,must_change_password") -> dict | None:
        result = self.client.table("app_users").select(columns).eq("id", user_id).limit(1).execute()
        return result.data[0] if result.data else None

    def create_user(self, row: dict) -> dict:
        result = self.client.table("app_users").insert(row).execute()
        return result.data[0]

    def update_user(self, user_id: str, updates: dict) -> dict | None:
        result = self.client.table("app_users").update(updates).eq("id", user_id).execute()
        return result.data[0] if result.data else None

    def list_users(self, limit: int) -> list[dict]:
        result = (
            self.client.table("app_users")
            .select(USER_PUBLIC_COLUMNS)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data

    def insert_refresh_token(self, row: dict) -> dict:
        result = self.client.table("refresh_tokens").insert(row).execute()
        return result.data[0]

    def get_refresh_token(self, token_hash: str) -> dict | None:
        result = (
            self.client.table("refresh_tokens")
            .select("id,user_id,expires_at,revoked_at")
            .eq("token_hash", token_hash)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def find_refresh_token_id(self, token_hash: str) -> str | None:
        result = self.client.table("refresh_tokens").select("id").eq("token_hash", token_hash).limit(1).execute()
        return result.data[0]["id"] if result.data else None

    def update_refresh_token(self, token_id: str, updates: dict) -> None:
        self.client.table("refresh_tokens").update(updates).eq("id", token_id).execute()

    def revoke_user_refresh_tokens(self, user_id: str, revoked_at: str) -> None:
        self.client.table("refresh_tokens").update({"revoked_at": revoked_at}).eq("user_id", user_id).is_("revoked_at", "null").execute()

    def insert_audit_event(self, row: dict) -> None:
        self.client.table("audit_events").insert(row).execute()

    def list_audit_events(self, limit: int) -> list[dict]:
        result = (
            self.client.table("audit_events")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data
