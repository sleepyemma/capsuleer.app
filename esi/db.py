import asyncio
import asyncpg
import logging
import weakref

from .types import (
    ABCSession,
    NoSuchCharacter,
    CharacterNeedsUpdated,
    AccessToken,
    Character,
)

logger = logging.getLogger(__name__)


class DatabaseSession(ABCSession):
    __slots__ = (
        "_pool",
        "_conn",
        "_account_id",
        "_character_id",
        "_character",
        "_access_token",
    )

    def __init__(self, pool, account_id, character_id):
        self._pool = pool
        self._account_id = account_id
        self._character_id = character_id

    async def initialize(self):
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(
                "SELECT access_token, refresh_token, access_token_expires, name "
                "FROM character WHERE character_id=$1 AND account_id=$2",
                self._character_id,
                self._account_id,
            )
        if record is None:
            raise NoSuchCharacter()
        if record["access_token"] is None:
            raise CharacterNeedsUpdated()
        self._access_token = AccessToken(
            record["access_token"],
            record["access_token_expires"].replace(tzinfo=None),
            record["refresh_token"],
        )
        self._character = Character(self._character_id, record["name"], True)
        return self

    @property
    def access_token(self):
        return self._access_token

    async def set_access_token(self, new_token):
        async with self._pool.acquire() as conn:
            if new_token is None:
                result = await conn.execute(
                    "UPDATE character SET "
                    "access_token=NULL, refresh_token=NULL, access_token_expires=NULL "
                    "WHERE character_id=$1",
                    self._character_id,
                )
            else:
                result = await conn.execute(
                    "UPDATE character SET "
                    "access_token=$1, refresh_token=$2, access_token_expires=$3 "
                    "WHERE character_id=$4",
                    new_token.access_token,
                    new_token.refresh_token,
                    new_token.expired_after,
                    self._character_id,
                )
            if result != "UPDATE 1":
                raise Exception(result)
        self._access_token = new_token

    @property
    def character(self):
        return self._character


class Database:
    """
    If several requests come in simultaneously for the same user to the same
    app-server, we will reuse their session object instead of create a new one.
    """

    __slots__ = "_connargs", "_cache", "_pool"

    def __init__(self, **kwargs):
        self._connargs = kwargs
        self._cache = weakref.WeakValueDictionary()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, a, b, c):
        await self._pool.close()

    async def connect(self):
        self._pool = await asyncpg.create_pool(**self._connargs)

    def get_session(self, account_id, character_id):
        try:
            return self._cache[(account_id, character_id)]
        except KeyError:
            pass

        task = asyncio.shield(self._get_session(account_id, character_id))
        self._cache[(account_id, character_id)] = task
        return task

    async def _get_session(self, account_id, character_id):
        new_session = DatabaseSession(self._pool, account_id, character_id)
        await new_session.initialize()
        return new_session

    @staticmethod
    def _insert_character(conn, account_id, character, token, owner_hash):
        return conn.fetchrow(
            "INSERT INTO character "
            "(character_id, account_id, access_token, refresh_token, "
            "access_token_expires, name, owner_hash)"
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            character.id,
            account_id,
            token.access_token,
            token.refresh_token,
            token.expired_after,
            character.name,
            owner_hash,
        )

    @staticmethod
    async def _insert_account(conn):
        record = await conn.fetchrow(
            "INSERT INTO account (id) VALUES (DEFAULT) RETURNING id"
        )
        return record["id"]

    async def get_characters(self, account_id):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT character_id, name, "
                "access_token IS NOT NULL as valid "
                "FROM character WHERE account_id=$1 "
                "ORDER BY create_time",
                account_id,
            )
        return [Character(r[0], r[1], r[2]) for r in rows]

    async def delete_character(self, account_id, character_id):
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                record = await conn.execute(
                    "DELETE FROM character WHERE account_id=$1 AND character_id=$2",
                    account_id,
                    character_id,
                )
                if record != "DELETE 1":
                    raise Exception(record)

    async def character_authorized(
        self, account_id, character, access_token, owner_hash
    ):
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                old_char = await conn.fetchrow(
                    "SELECT account_id, owner_hash "
                    "FROM character WHERE character_id=$1",
                    character.id,
                )
                if old_char is None:
                    # character has never logged in before.
                    if account_id is None:
                        # Create an account
                        account_id = await self._insert_account(conn)
                        logger.info("new user %d", account_id)
                    await self._insert_character(
                        conn, account_id, character, access_token, owner_hash
                    )
                    logger.info("new character %d", character.id)
                    return account_id

                # Character has logged in before.
                if account_id is None:
                    if old_char["owner_hash"] == owner_hash:
                        # success, fallthru to update
                        logger.info("recognized character %d", character.id)
                        account_id = old_char["account_id"]
                    else:
                        # This character changed hands. Break old ties
                        logger.info(
                            "character %d owner hash changed from %r to %r",
                            character.id,
                            old_char["owner_hash"],
                            owner_hash,
                        )
                        account_id = await self._insert_account(conn)

                # Does this character need to have its ownership updated?
                if old_char["account_id"] != account_id:
                    logger.info(
                        "character %d owner changing from %d to %d",
                        character.id,
                        old_char["account_id"],
                        account_id,
                    )

                record = await conn.execute(
                    "UPDATE character SET "
                    "access_token=$1, refresh_token=$2, access_token_expires=$3, "
                    "account_id=$4 "
                    "WHERE character_id=$5",
                    access_token.access_token,
                    access_token.refresh_token,
                    access_token.expired_after,
                    account_id,
                    character.id,
                )
                if record != "UPDATE 1":
                    raise Exception(record)

        return account_id
