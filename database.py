from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone

class FileDB:
    def __init__(self, uri: str, db_name: str):
        self.uri = uri
        self.db_name = db_name
        self.client = None
        self.files = None

    async def connect(self):
        self.client = AsyncIOMotorClient(
            self.uri,
            maxPoolSize=50,
            minPoolSize=5,
            serverSelectionTimeoutMS=5000,
        )
        await self.client.admin.command("ping")
        self.files = self.client[self.db_name]["files"]
        await self.files.create_index("token", unique=True)

    async def save_file(self, token, file_id, unique_id, protect=False):
        await self.files.update_one(
            {"token": token},
            {"$set": {
                "token": token,
                "file_id": file_id,
                "unique_id": unique_id,
                "protect": protect,
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    async def get_file(self, token):
        return await self.files.find_one({"token": token})

    async def count_files(self):
        return await self.files.count_documents({})
