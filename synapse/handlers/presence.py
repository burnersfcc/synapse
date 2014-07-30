# -*- coding: utf-8 -*-
from twisted.internet import defer

from synapse.api.errors import SynapseError, AuthError
from synapse.api.constants import PresenceState

from ._base import BaseHandler

import logging


logger = logging.getLogger(__name__)


# TODO(paul): Maybe there's one of these I can steal from somewhere
def partition(l, func):
    """Partition the list by the result of func applied to each element."""
    ret = {}

    for x in l:
        key = func(x)
        if key not in ret:
            ret[key] = []
        ret[key].append(x)

    return ret


def partitionbool(l, func):
    def boolfunc(x):
        return bool(func(x))

    ret = partition(l, boolfunc)
    return ret.get(True, []), ret.get(False, [])


class PresenceHandler(BaseHandler):

    def __init__(self, hs):
        super(PresenceHandler, self).__init__(hs)

        self.homeserver = hs

        distributor = hs.get_distributor()
        distributor.observe("registered_user", self.registered_user)
        distributor.observe("received_edu", self._received_edu)

        self.federation = hs.get_replication_layer()

        # IN-MEMORY store, mapping local userparts to sets of local users to
        # be informed of state changes.
        self._local_pushmap = {}
        # map local users to sets of remote /domain names/ who are interested
        # in them
        self._remote_sendmap = {}
        # map remote users to sets of local users who're interested in them
        self._remote_recvmap = {}

    def registered_user(self, user):
        self.store.create_presence(user.localpart)

    def _received_edu(self, origin, edu_type, content):
        hs = self.homeserver

        # TODO(paul): Maybe this suggests a nicer interface of
        #   federation.register_edu_handler("sy.presence_invite", callable...)

        if edu_type == "sy.presence":
            return self.incoming_presence(pushes=content["push"])
        elif edu_type == "sy.presence_invite":
            return self.invite_presence(
                observed_user=hs.parse_userid(content["observed_user"]),
                observer_user=hs.parse_userid(content["observer_user"]),
            )
        elif edu_type == "sy.presence_accept":
            return self.accept_presence(
                observed_user=hs.parse_userid(content["observed_user"]),
                observer_user=hs.parse_userid(content["observer_user"]),
            )
        else:
            return defer.succeed(None)

    @defer.inlineCallbacks
    def get_state(self, target_user, auth_user):
        if target_user.is_mine:
            # TODO(paul): Only allow local users who are presence-subscribed
            state = yield self.store.get_presence_state(
                    target_user.localpart)

            defer.returnValue(state)
        else:
            # TODO(paul): Look up in local cache(?) or ask server-server API?
            defer.returnValue({"state": 0, "status_msg": ""})

    @defer.inlineCallbacks
    def set_state(self, target_user, auth_user, state):
        if not target_user.is_mine:
            raise SynapseError(400, "User is not hosted on this Home Server")

        if target_user != auth_user:
            raise AuthError(400, "Cannot set another user's displayname")

        # TODO(paul): Sanity-check 'state'. Needs 'status' of suitable value
        # and optional status_msg.

        logger.debug("Updating presence state of %s to %s",
                target_user.localpart, state["state"])

        oldstate = yield self.store.set_presence_state(target_user.localpart,
                state
        )

        now_online = state["state"] != PresenceState.OFFLINE
        was_online = oldstate != PresenceState.OFFLINE

        if now_online and not was_online:
            self.start_polling_presence(target_user)
        elif not now_online and was_online:
            self.stop_polling_presence(target_user)

        # TODO(paul): perform a presence push as part of start/stop poll so
        #   we don't have to do this all the time
        self.push_presence(target_user, state=state)

    @defer.inlineCallbacks
    def send_invite(self, observer_user, observed_user):
        if not observer_user.is_mine:
            raise SynapseError(400, "User is not hosted on this Home Server")

        yield self.store.add_presence_list_pending(
                observer_user.localpart, observed_user.to_string())

        if observed_user.is_mine:
            yield self.invite_presence(observed_user, observer_user)
        else:
            yield self.federation.send_edu(
                    destination=observed_user.domain,
                    edu_type="sy.presence_invite",
                    content={
                        "observed_user": observed_user.to_string(),
                        "observer_user": observer_user.to_string(),
                    })

    @defer.inlineCallbacks
    def invite_presence(self, observed_user, observer_user):
        # TODO(paul): Eventually we'll ask the user's permission for this
        # before accepting. For now just accept any invite request
        yield self.store.allow_presence_inbound(
                observed_user.localpart, observer_user.to_string())

        if observer_user.is_mine:
            yield self.accept_presence(observed_user, observer_user)
        else:
            yield self.federation.send_edu(
                    destination=observer_user.domain,
                    edu_type="sy.presence_accept",
                    content={
                        "observed_user": observed_user.to_string(),
                        "observer_user": observer_user.to_string(),
                    })

    @defer.inlineCallbacks
    def accept_presence(self, observed_user, observer_user):
        yield self.store.set_presence_list_accepted(
                observer_user.localpart, observed_user.to_string())

        self.start_polling_presence(observer_user, target_user=observed_user)

    @defer.inlineCallbacks
    def start_polling_presence(self, user, target_user=None, state=None):
        logger.debug("Start polling for presence from %s", user)

        if target_user:
            target_users = [target_user]
        else:
            target_user_ids = yield self.store.get_presence_list(user.localpart)
            target_users = [self.hs.parse_userid(x) for x in target_user_ids]

        if state is None:
            state = yield self.store.get_presence_state(user.localpart)

        localusers, remoteusers = partitionbool(target_users,
                lambda u: u.is_mine)

        deferreds = []
        for target_user in localusers:
            deferreds.append(self._start_polling_local(user, target_user))

        # TODO(paul): remotes

        yield defer.DeferredList(deferreds)

    @defer.inlineCallbacks
    def _start_polling_local(self, user, target_user):
        target_localpart = target_user.localpart

        # TODO(paul) permissions checks

        if target_localpart not in self._local_pushmap:
            self._local_pushmap[target_localpart] = set()

        self._local_pushmap[target_localpart].add(user)

        target_state = yield self.store.get_presence_state(target_localpart)

        yield self.push_update_to_clients(
                observer_user=user,
                observed_user=target_user,
                state=target_state,
        )

    def stop_polling_presence(self, user, target_user=None):
        logger.debug("Stop polling for presence from %s", user)

        if not target_user or target_user.is_mine:
            self._stop_polling_local(user, target_user=target_user)

        # TODO(paul): remotes

    def _stop_polling_local(self, user, target_user):
        for localpart in self._local_pushmap.keys():
            if target_user and localpart != target_user.localpart:
                continue

            if user in self._local_pushmap[localpart]:
                self._local_pushmap[localpart].remove(user)

            if not self._local_pushmap[localpart]:
                del self._local_pushmap[localpart]

    @defer.inlineCallbacks
    def push_presence(self, user, state=None):
        assert(user.is_mine)

        logger.debug("Pushing presence update from %s", user)

        localusers = self._local_pushmap.get(user.localpart, [])
        remotedomains = self._remote_sendmap.get(user.localpart, [])

        if not localusers and not remotedomains:
            defer.returnValue(None)

        if state is None:
            state = yield self.store.get_presence_state(user.localpart)

        deferreds = []
        for u in localusers:
            deferreds.append(self.push_update_to_clients(
                observer_user=u,
                observed_user=user,
                state=state
            ))

        for domain in remotedomains:
            deferreds.append(self.federation.send_edu(
                destination=domain,
                edu_type="sy.presence",
                content={
                    "push": [
                        dict(user_id=user.to_string(), **state),
                    ],
                }
            ))

        yield defer.DeferredList(deferreds)

    @defer.inlineCallbacks
    def incoming_presence(self, pushes):
        deferreds = []

        for push in pushes:
            user = self.hs.parse_userid(push["user_id"])

            logger.debug("Incoming presence update from %s", user)

            if user not in self._remote_recvmap:
                break

            observers = self._remote_recvmap[user]
            state = dict(push)
            del state["user_id"]

            for observer_user in observers:
                deferreds.append(self.push_update_to_clients(
                        observer_user=observer_user,
                        observed_user=user,
                        state=state
                ))

        yield defer.DeferredList(deferreds)

    def push_update_to_clients(self, observer_user, observed_user, state):
        # TODO(paul)
        pass
