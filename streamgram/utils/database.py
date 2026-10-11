#(c) Adarsh-Goel
import asyncio
import datetime
import motor.motor_asyncio
import logging
logger = logging.getLogger("streamgram.utils.database")
import sys
import pymongo.errors
from streamgram.vars import Var

_client = None


def get_client():
    """One Motor client for the whole process, shared by every Database / AccessDB."""
    global _client
    if _client is None:
        logger.debug("Creating the shared Motor client (schema=%s, host=%s)", Var.MONGO_SCHEMA, Var.MONGO_HOST)
        _client = motor.motor_asyncio.AsyncIOMotorClient(get_mongo_uri())
    else:
        logger.debug("Reusing the shared Motor client")
    return _client


def get_mongo_uri() -> str:
    """Builds and returns the MongoDB connection URI from environment variables."""
    # Never log the full URI: it embeds MONGO_USERNAME/MONGO_PASSWORD.
    # The user:pass@ segment is only included when a username is actually set -- an
    # authless local/dev Mongo (MONGO_USERNAME/PASSWORD both blank, e.g. a plain `mongo`
    # container with no auth configured) would otherwise produce "mongodb://:@host:port/",
    # which pymongo correctly rejects as InvalidURI ("The empty string is not valid
    # username"). Confirmed live via docker compose against an unauthenticated mongo:7.
    auth = f"{Var.MONGO_USERNAME}:{Var.MONGO_PASSWORD}@" if Var.MONGO_USERNAME else ""
    if Var.MONGO_SCHEMA == "mongodb+srv":
        uri = f"{Var.MONGO_SCHEMA}://{auth}{Var.MONGO_HOST}/?retryWrites=true&w=majority&tls=true"
    else:
        uri = f"{Var.MONGO_SCHEMA}://{auth}{Var.MONGO_HOST}:{Var.MONGO_PORT}/"
    logger.debug("Built Mongo URI (schema=%s, host=%s, credentials redacted)", Var.MONGO_SCHEMA, Var.MONGO_HOST)
    return uri

class Database:
    _instances = {}

    @classmethod
    def shared(cls, database_name):
        """One Database per name for the whole process. The first call schedules the connection check."""
        inst = cls._instances.get(database_name)
        if inst is None:
            logger.debug("Creating a new Database instance for %r", database_name)
            inst = cls._instances[database_name] = cls(get_mongo_uri(), database_name)
            try:
                task = asyncio.get_event_loop().create_task(inst.initialize())
                task.add_done_callback(
                    lambda t: not t.cancelled() and t.exception()
                    and logger.critical(f"Database initialization error: {t.exception()}")
                )
            except Exception as e:
                logger.critical(f"Critical error occurred during database initialization: {e}")
                sys.exit(1)
        return inst

    def __init__(self, uri, database_name):
        # Store the client and database names
        self._client = get_client() if uri == get_mongo_uri() else motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users

    async def initialize(self):
        try:
            # Test the connection by performing a simple ping operation
            await self._client.admin.command('ping')
            try:
                await self.col.create_index("id", unique=True)
            except Exception as e:
                logger.warning(f"Could not create the unique index on users.id: {e}")
            logger.info(f"Database initialized with URI: {self._client.address} and Database: {self.db.name}")
        except pymongo.errors.ServerSelectionTimeoutError as e:
            logger.critical(f"Failed to connect to MongoDB server. URI: {self._client.address}, Error: {e}")
            logger.critical("Unable to establish a database connection. Exiting the program.")
            sys.exit(1)
        except pymongo.errors.ConnectionFailure as e:
            logger.critical(f"Failed to connect to MongoDB server. URI: {self._client.address}, Error: {e}")
            logger.critical("Unable to establish a database connection. Exiting the program.")
            sys.exit(1)
        except Exception as e:
            logger.critical(f"An unexpected error occurred during MongoDB initialization. Error: {e}")
            sys.exit(1)

    def new_user(self, id):
        logger.debug(f"Creating new user with ID: {id}")
        return dict(
            id=id,
            join_date=datetime.date.today().isoformat()
        )

    async def add_user(self, id):
        try:
            user = self.new_user(id)
            logger.debug(f"Adding new user with ID: {id}")
            await self.col.insert_one(user)
            logger.info(f"User with ID: {id} successfully added to the database")
        except pymongo.errors.ServerSelectionTimeoutError as e:
            logger.error(f"Database timeout while adding user with ID: {id}. Error: {e}")
        except Exception as e:
            logger.error(f"Error adding user with ID: {id}. Error: {e}")

    async def add_user_pass(self, id, ag_pass):
        try:
            logger.debug(f"Adding user with password for ID: {id}")
            await self.add_user(int(id))
            await self.col.update_one({'id': int(id)}, {'$set': {'ag_p': ag_pass}})
            logger.info(f"Password for user with ID: {id} has been successfully updated")
        except Exception as e:
            logger.error(f"Error updating password for user with ID: {id}. Error: {e}")

    async def get_user_pass(self, id):
        try:
            logger.debug(f"Fetching password for user with ID: {id}")
            user_pass = await self.col.find_one({'id': int(id)})
            if user_pass:
                logger.debug(f"Password retrieved for user with ID: {id}")
                return user_pass.get("ag_p", None)
            else:
                logger.debug(f"User with ID: {id} not found")
                return None
        except Exception as e:
            logger.error(f"Error retrieving password for user with ID: {id}. Error: {e}")
            return None

    async def is_user_exist(self, id):
        try:
            logger.debug(f"Checking existence of user with ID: {id}")
            user = await self.col.find_one({'id': int(id)})
            exists = bool(user)
            if exists:
                logger.debug(f"User with ID: {id} exists in the database")
            else:
                logger.debug(f"User with ID: {id} does not exist")
            return exists
        except pymongo.errors.ServerSelectionTimeoutError as e:
            logger.error(f"Database timeout while checking existence of user with ID: {id}. Error: {e}")
            return False
        except Exception as e:
            logger.error(f"Error checking existence for user with ID: {id}. Error: {e}")
            return False

    async def total_users_count(self):
        try:
            logger.debug(f"Counting total users in the database")
            count = await self.col.count_documents({})
            logger.debug(f"Total number of users: {count}")
            return count
        except Exception as e:
            logger.error(f"Error counting total users. Error: {e}")
            return 0

    async def get_all_users(self):
        try:
            logger.debug("Retrieving all users from the database")
            all_users = self.col.find({})
            logger.debug("All users retrieved successfully")
            return all_users
        except Exception as e:
            logger.error(f"Error retrieving all users. Error: {e}")
            return None

    async def delete_user(self, user_id):
        try:
            logger.debug(f"Deleting user with ID: {user_id}")
            await self.col.delete_many({'id': int(user_id)})
            logger.info(f"User with ID: {user_id} successfully deleted from the database")
        except Exception as e:
            logger.error(f"Error deleting user with ID: {user_id}. Error: {e}")
