# -*- coding: utf-8 -*-
# Copyright 2017 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer

from ._base import SQLBaseStore


class EndToEndRoomKeyStore(SQLBaseStore):

    @defer.inlineCallbacks
    def get_e2e_room_key(self, user_id, version, room_id, session_id):
        """Get the encrypted E2E room key for a given session from a given
        backup version of room_keys.  We only store the 'best' room key for a given
        session at a given time, as determined by the handler.

        Args:
            user_id(str): the user whose backup we're querying
            version(str): the version ID of the backup for the set of keys we're querying
            room_id(str): the ID of the room whose keys we're querying.
                This is a bit redundant as it's implied by the session_id, but
                we include for consistency with the rest of the API.
            session_id(str): the session whose room_key we're querying.

        Returns:
            A deferred dict giving the session_data and message metadata for
            this room key.
        """

        row = yield self._simple_select_one(
            table="e2e_room_keys",
            keyvalues={
                "user_id": user_id,
                "version": version,
                "room_id": room_id,
                "session_id": session_id,
            },
            retcols=(
                "first_message_index",
                "forwarded_count",
                "is_verified",
                "session_data",
            ),
            desc="get_e2e_room_key",
        )

        defer.returnValue(row)

    def set_e2e_room_key(self, user_id, version, room_id, session_id, room_key):
        """Replaces or inserts the encrypted E2E room key for a given session in
        a given backup

        Args:
            user_id(str): the user whose backup we're setting
            version(str): the version ID of the backup we're updating
            room_id(str): the ID of the room whose keys we're setting
            session_id(str): the session whose room_key we're setting
            room_key(dict): the room_key being set
        Raises:
            StoreError if stuff goes wrong, probably
        """

        yield self._simple_upsert(
            table="e2e_room_keys",
            keyvalues={
                "user_id": user_id,
                "room_id": room_id,
                "session_id": session_id,
            },
            values={
                "version": version,
                "first_message_index": room_key['first_message_index'],
                "forwarded_count": room_key['forwarded_count'],
                "is_verified": room_key['is_verified'],
                "session_data": room_key['session_data'],
            },
            lock=False,
        )

    @defer.inlineCallbacks
    def get_e2e_room_keys(
        self, user_id, version, room_id=None, session_id=None
    ):
        """Bulk get the E2E room keys for a given backup, optionally filtered to a given
        room, or a given session.

        Args:
            user_id(str): the user whose backup we're querying
            version(str): the version ID of the backup for the set of keys we're querying
            room_id(str): Optional. the ID of the room whose keys we're querying, if any.
                If not specified, we return the keys for all the rooms in the backup.
            session_id(str): Optional. the session whose room_key we're querying, if any.
                If specified, we also require the room_id to be specified.
                If not specified, we return all the keys in this version of
                the backup (or for the specified room)

        Returns:
            A deferred list of dicts giving the session_data and message metadata for
            these room keys.
        """

        keyvalues = {
            "user_id": user_id,
            "version": version,
        }
        if room_id:
            keyvalues['room_id'] = room_id
            if session_id:
                keyvalues['session_id'] = session_id

        rows = yield self._simple_select_list(
            table="e2e_room_keys",
            keyvalues=keyvalues,
            retcols=(
                "user_id",
                "room_id",
                "session_id",
                "first_message_index",
                "forwarded_count",
                "is_verified",
                "session_data",
            ),
            desc="get_e2e_room_keys",
        )

        sessions = {}
        for row in rows:
            room_entry = sessions['rooms'].setdefault(row['room_id'], {"sessions": {}})
            room_entry['sessions'][row['session_id']] = {
                "first_message_index": row["first_message_index"],
                "forwarded_count": row["forwarded_count"],
                "is_verified": row["is_verified"],
                "session_data": row["session_data"],
            }

        defer.returnValue(sessions)

    @defer.inlineCallbacks
    def delete_e2e_room_keys(
        self, user_id, version, room_id=None, session_id=None
    ):
        """Bulk delete the E2E room keys for a given backup, optionally filtered to a given
        room or a given session.

        Args:
            user_id(str): the user whose backup we're deleting from
            version(str): the version ID of the backup for the set of keys we're deleting
            room_id(str): Optional. the ID of the room whose keys we're deleting, if any.
                If not specified, we delete the keys for all the rooms in the backup.
            session_id(str): Optional. the session whose room_key we're querying, if any.
                If specified, we also require the room_id to be specified.
                If not specified, we delete all the keys in this version of
                the backup (or for the specified room)

        Returns:
            A deferred of the deletion transaction
        """

        keyvalues = {
            "user_id": user_id,
            "version": version,
        }
        if room_id:
            keyvalues['room_id'] = room_id
            if session_id:
                keyvalues['session_id'] = session_id

        yield self._simple_delete(
            table="e2e_room_keys",
            keyvalues=keyvalues,
            desc="delete_e2e_room_keys",
        )

    def get_e2e_room_keys_version_info(self, user_id, version=None):
        """Get info metadata about a version of our room_keys backup.

        Args:
            user_id(str): the user whose backup we're querying
            version(str): Optional. the version ID of the backup we're querying about
                If missing, we return the information about the current version.
        Raises:
            StoreError: with code 404 if there are no e2e_room_keys_versions present
        Returns:
            A deferred dict giving the info metadata for this backup version
        """

        def _get_e2e_room_keys_version_info_txn(txn):
            if version is None:
                txn.execute(
                    "SELECT MAX(version) FROM e2e_room_keys_versions WHERE user_id=?",
                    (user_id,)
                )
                version = txn.fetchone()[0]

            return self._simple_select_one_txn(
                table="e2e_room_keys_versions",
                keyvalues={
                    "user_id": user_id,
                    "version": version,
                },
                retcols=(
                    "user_id",
                    "version",
                    "algorithm",
                    "auth_data",
                ),
            )

        return self.runInteraction(
            desc="get_e2e_room_keys_version_info",
            _get_e2e_room_keys_version_info_txn
        )

    def create_e2e_room_keys_version(self, user_id, info):
        """Atomically creates a new version of this user's e2e_room_keys store
        with the given version info.

        Args:
            user_id(str): the user whose backup we're creating a version
            info(dict): the info about the backup version to be created

        Returns:
            A deferred string for the newly created version ID
        """

        def _create_e2e_room_keys_version_txn(txn):
            txn.execute(
                "SELECT MAX(version) FROM e2e_room_keys_versions WHERE user_id=?",
                (user_id,)
            )
            current_version = txn.fetchone()[0]
            if current_version is None:
                current_version = 0

            new_version = current_version + 1

            self._simple_insert_txn(
                txn,
                table="e2e_room_keys_versions",
                values={
                    "user_id": user_id,
                    "version": new_version,
                    "algorithm": info["algorithm"],
                    "auth_data": info["auth_data"],
                },
            )

            return new_version

        return self.runInteraction(
            "create_e2e_room_keys_version_txn", _create_e2e_room_keys_version_txn
        )

    @defer.inlineCallbacks
    def delete_e2e_room_keys_version(self, user_id, version):
        """Delete a given backup version of the user's room keys.
        Doesn't delete their actual key data.

        Args:
            user_id(str): the user whose backup version we're deleting
            version(str): the ID of the backup version we're deleting
        """

        keyvalues = {
            "user_id": user_id,
            "version": version,
        }

        yield self._simple_delete(
            table="e2e_room_keys_versions",
            keyvalues=keyvalues,
            desc="delete_e2e_room_keys_version",
        )