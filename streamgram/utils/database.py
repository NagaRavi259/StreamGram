#(c) Adarsh-Goel
import datetime
import motor.motor_asyncio
import logging
import sys
import pymongo.errors

class Database:
    def __init__(self, uri, database_name):
        # Store the client and database names
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users

    async def initialize(self):
        try:
            # Test the connection by performing a simple ping operation
            await self._client.admin.command('ping')
            logging.info(f"Database initialized with URI: {self._client.address} and Database: {self.db.name}")
        except pymongo.errors.ServerSelectionTimeoutError as e:
            logging.critical(f"Failed to connect to MongoDB server. URI: {self._client.address}, Error: {e}")
            logging.critical("Unable to establish a database connection. Exiting the program.")
            print("Unable to establish a database connection. Exiting the program.")
            sys.exit(1)
        except pymongo.errors.ConnectionFailure as e:
            logging.critical(f"Failed to connect to MongoDB server. URI: {self._client.address}, Error: {e}")
            logging.critical("Unable to establish a database connection. Exiting the program.")
            print("Unable to establish a database connection. Exiting the program.")
            sys.exit(1)
        except Exception as e:
            logging.critical(f"An unexpected error occurred during MongoDB initialization. Error: {e}")
            print("Unable to establish a database connection. Exiting the program.")
            sys.exit(1)

    def new_user(self, id):
        logging.info(f"Creating new user with ID: {id}")
        return dict(
            id=id,
            join_date=datetime.date.today().isoformat()
        )

    async def add_user(self, id):
        try:
            user = self.new_user(id)
            logging.info(f"Adding new user with ID: {id}")
            await self.col.insert_one(user)
            logging.info(f"User with ID: {id} successfully added to the database")
        except pymongo.errors.ServerSelectionTimeoutError as e:
            logging.error(f"Database timeout while adding user with ID: {id}. Error: {e}")
        except Exception as e:
            logging.error(f"Error adding user with ID: {id}. Error: {e}")

    async def add_user_pass(self, id, ag_pass):
        try:
            logging.info(f"Adding user with password for ID: {id}")
            await self.add_user(int(id))
            await self.col.update_one({'id': int(id)}, {'$set': {'ag_p': ag_pass}})
            logging.info(f"Password for user with ID: {id} has been successfully updated")
        except Exception as e:
            logging.error(f"Error updating password for user with ID: {id}. Error: {e}")

    async def get_user_pass(self, id):
        try:
            logging.info(f"Fetching password for user with ID: {id}")
            user_pass = await self.col.find_one({'id': int(id)})
            if user_pass:
                logging.info(f"Password retrieved for user with ID: {id}")
                return user_pass.get("ag_p", None)
            else:
                logging.warning(f"User with ID: {id} not found")
                return None
        except Exception as e:
            logging.error(f"Error retrieving password for user with ID: {id}. Error: {e}")
            return None

    async def is_user_exist(self, id):
        try:
            logging.info(f"Checking existence of user with ID: {id}")
            user = await self.col.find_one({'id': int(id)})
            exists = bool(user)
            if exists:
                logging.info(f"User with ID: {id} exists in the database")
            else:
                logging.warning(f"User with ID: {id} does not exist")
            return exists
        except pymongo.errors.ServerSelectionTimeoutError as e:
            logging.error(f"Database timeout while checking existence of user with ID: {id}. Error: {e}")
            return False
        except Exception as e:
            logging.error(f"Error checking existence for user with ID: {id}. Error: {e}")
            return False

    async def total_users_count(self):
        try:
            logging.info(f"Counting total users in the database")
            count = await self.col.count_documents({})
            logging.info(f"Total number of users: {count}")
            return count
        except Exception as e:
            logging.error(f"Error counting total users. Error: {e}")
            return 0

    async def get_all_users(self):
        try:
            logging.info("Retrieving all users from the database")
            all_users = self.col.find({})
            logging.info("All users retrieved successfully")
            return all_users
        except Exception as e:
            logging.error(f"Error retrieving all users. Error: {e}")
            return None

    async def delete_user(self, user_id):
        try:
            logging.info(f"Deleting user with ID: {user_id}")
            await self.col.delete_many({'id': int(user_id)})
            logging.info(f"User with ID: {user_id} successfully deleted from the database")
        except Exception as e:
            logging.error(f"Error deleting user with ID: {user_id}. Error: {e}")
