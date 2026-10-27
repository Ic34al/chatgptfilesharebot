from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone


class FileDB:
    """Original file-share storage (collection ``files``). API kept backward compatible."""

    def __init__(self, uri: str, db_name: str):
        self.uri = uri
        self.db_name = db_name
        self.client = None
        self.db = None
        self.files = None

    async def connect(self, client: AsyncIOMotorClient = None):
        self.client = client or AsyncIOMotorClient(
            self.uri,
            maxPoolSize=50,
            minPoolSize=5,
            serverSelectionTimeoutMS=5000,
            tz_aware=True,
        )
        await self.client.admin.command("ping")
        self.db = self.client[self.db_name]
        self.files = self.db["files"]
        await self.files.create_index("token", unique=True)

    async def save_file(self, token, file_id, unique_id, protect=False, **extra):
        await self.files.update_one(
            {"token": token},
            {"$set": {
                "token": token,
                "file_id": file_id,
                "unique_id": unique_id,
                "protect": protect,
                "updated_at": datetime.now(timezone.utc),
                **extra,
            }},
            upsert=True,
        )

    async def get_file(self, token):
        return await self.files.find_one({"token": token})

    async def find_by_unique_id(self, unique_id):
        if not unique_id:
            return None
        return await self.files.find_one({"unique_id": unique_id, "token": {"$exists": True}},
                                         sort=[("updated_at", -1)])

    async def count_files(self):
        return await self.files.count_documents({})
