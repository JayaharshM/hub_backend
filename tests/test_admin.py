import pytest
import pytest_asyncio
import csv
import io
from fastapi import status
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch
from sqlalchemy import select, text

from app.main import app
from app.database import AsyncSessionLocal
from app.models.user import User
from app.auth.security.password import hash_password
from app.auth.security.jwt import create_access_token
from app.config import settings

@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    from app.database import engine
    from app.redis import redis_client
    await engine.dispose()
    try:
        await redis_client.aclose()
    except Exception:
        pass
    async with AsyncSessionLocal() as session:
        await session.execute(text("TRUNCATE TABLE users CASCADE;"))
        await session.commit()
    yield

@pytest_asyncio.fixture
def admin_headers():
    token = create_access_token({"sub": "00000000-0000-0000-0000-000000000000", "role": "admin"})
    return {"Authorization": f"Bearer {token}"}

@pytest.mark.asyncio
async def test_bulk_create_users_success(admin_headers):
    # Prepare dummy CSV
    csv_data = "email,full_name,phone\nbulk1@example.com,Bulk User One,1234567890\nbulk2@example.com,Bulk User Two,0987654321\n"
    files = {"file": ("users.csv", csv_data, "text/csv")}

    # Mock the httpx post call to notify service inside admin.py
    with patch("app.routers.admin.httpx.AsyncClient") as mock_client_cls:
        import httpx
        mock_client = AsyncMock()
        mock_response = httpx.Response(status_code=202, json={"status": "processing"})
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/admin/users/bulk",
                headers=admin_headers,
                files=files
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert data["status"] == "processing"

        # Verify users were added to the DB
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(User).where(User.email.in_(["bulk1@example.com", "bulk2@example.com"])))
            users = result.scalars().all()
            assert len(users) == 2
            assert {u.email for u in users} == {"bulk1@example.com", "bulk2@example.com"}

        # Verify notify service /bulk endpoint was called
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        url = args[0]
        assert url.endswith("/notify/bulk")
        payload = kwargs["json"]
        assert payload["channel"] == "email"
        assert len(payload["recipients"]) == 2
        assert payload["recipients"][0]["recipient"] == "bulk1@example.com"
        assert payload["recipients"][1]["recipient"] == "bulk2@example.com"
        assert "Your CixioHub Credentials" in payload["recipients"][0]["subject"]
