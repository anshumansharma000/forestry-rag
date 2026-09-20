from supabase import Client

from services.storage import supabase_client

USER_PUBLIC_COLUMNS = "id,email,full_name,role,is_active,must_change_password,last_login_at,metadata,created_at,updated_at"


class AuthRepository:
    def __init__(self, client: Client | None = None):
        self.client = client or supabase_client()

    def get_user_for_auth(self, email: str) -> dict | None:
        result = (
            self.client.table("app_users")
            .select(f"{USER_PUBLIC_COLUMNS},password_hash,token_version")
            .eq("email", email.strip().lower())
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_user_by_id(
        self, user_id: str, columns: str = "id,email,full_name,role,is_active,must_change_password,token_version"
    ) -> dict | None:
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

    def issue_refresh_token(self, row: dict, token_version: int) -> dict | None:
        return self.client.rpc("issue_auth_refresh_token", {
            "p_user_id": row["user_id"], "p_version": token_version, "p_hash": row["token_hash"],
            "p_expires_at": row["expires_at"], "p_ip": row["ip_address"], "p_agent": row["user_agent"],
            "p_metadata": row["metadata"],
        }).execute().data

    def rotate_refresh_token(self, old_hash: str, new_hash: str, expires_at: str, ip: str | None, agent: str | None) -> dict | None:
        return self.client.rpc("rotate_auth_refresh_token", {
            "p_old_hash": old_hash, "p_new_hash": new_hash, "p_expires_at": expires_at,
            "p_ip": ip, "p_agent": agent,
        }).execute().data

    def change_password(self, user_id: str, expected_hash: str | None, new_hash: str, must_change: bool) -> dict | None:
        return self.client.rpc("change_auth_password", {
            "p_user_id": user_id, "p_expected_hash": expected_hash, "p_new_hash": new_hash,
            "p_must_change": must_change,
        }).execute().data

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
