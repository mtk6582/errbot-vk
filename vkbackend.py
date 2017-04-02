import logging
import sys

from errbot.backends.base import RoomError, Identifier, Person, RoomOccupant, ONLINE, Room, Message
from errbot.core import ErrBot
from errbot.rendering import text
from errbot.rendering.ansiext import enable_format, TEXT_CHRS
from errbot.utils import rate_limited

import functools

import requests

# Can't use __name__ because of Yapsy
log = logging.getLogger('errbot.backends.VK')

MESSAGE_SIZE_LIMIT = 50000
rate_limit = 3  # one message send per {rate_limit} seconds

try:
    import vk_api as vk
except ImportError:
    log.exception("Could not start the VK back-end")
    log.fatal(
        "You need to install the vk_api package in order "
        "to use the VK back-end. "
        "You should be able to install this package using: "
        "pip install vk_api"
    )
    sys.exit(1)


class RoomsNotSupportedError(RoomError):
    def __init__(self, message=None):
        if message is None:
            message = (
                "Room operations are not supported on VK. "
            )
        super().__init__(message)


class _Equals(object):
    def __init__(self, o):
        self.obj = o

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0


def lru_cache_ignoring_first_argument(*args, **kwargs):
    lru_decorator = functools.lru_cache(*args, **kwargs)

    def decorator(f):
        @lru_decorator
        def helper(arg1, *args, **kwargs):
            arg1 = arg1.obj
            return f(arg1, *args, **kwargs)

        @functools.wraps(f)
        def function(arg1, *args, **kwargs):
            arg1 = _Equals(arg1)
            return helper(arg1, *args, **kwargs)

        return function

    return decorator


class VKBotFilter(object):
    """
    This is a filter for the logging library that filters the
    "No new updates found." log message generated by VK.bot.

    This is an INFO-level log message that gets logged for every
    getUpdates() call where there are no new messages, so is way
    too verbose.
    """

    @staticmethod
    def filter(record):
        if record.getMessage() == "No new updates found.":
            return 0


class VKIdentifier(Identifier):
    def __init__(self, id):
        self._id = id

    @property
    def id(self):
        return self._id

    def __unicode__(self):
        return str(self._id)

    def __eq__(self, other):
        return self._id == other.id

    __str__ = __unicode__

    aclattr = id


class VKPerson(VKIdentifier, Person):
    def __init__(self, id, first_name=None, last_name=None, username=None):
        super().__init__(id)

        self._first_name = first_name
        self._last_name = last_name
        self._username = username

    @property
    def id(self):
        return self._id

    @property
    def first_name(self):
        return self._first_name

    @property
    def last_name(self):
        return self._last_name

    @property
    def fullname(self):
        fullname = self.first_name
        if self.last_name is not None:
            fullname += " " + self.last_name
        return fullname

    @property
    def username(self):
        return self._username

    @property
    def client(self):
        return None

    person = id
    nick = username


class VKRoom(VKIdentifier, Room):
    def __init__(self, id, title=None):
        super().__init__(id)
        self._title = title

    @property
    def id(self):
        return self._id

    @property
    def title(self):
        """Return the groupchat title (only applies to groupchats)"""
        return self._title

    def join(self, username: str = None, password: str = None):
        raise RoomsNotSupportedError()

    def create(self):
        raise RoomsNotSupportedError()

    def leave(self, reason: str = None):
        raise RoomsNotSupportedError()

    def destroy(self):
        raise RoomsNotSupportedError()

    @property
    def joined(self):
        raise RoomsNotSupportedError()

    @property
    def exists(self):
        raise RoomsNotSupportedError()

    @property
    def topic(self):
        raise RoomsNotSupportedError()

    @property
    def occupants(self):
        raise RoomsNotSupportedError()

    def invite(self, *args):
        raise RoomsNotSupportedError()


class VKMUCOccupant(VKPerson, RoomOccupant):
    """
    This class represents a person inside a MUC.
    """

    def __init__(self, id, room, first_name=None, last_name=None, username=None):
        super().__init__(id=id, first_name=first_name, last_name=last_name, username=username)
        self._room = room

    @property
    def room(self):
        return self._room

    @property
    def username(self):
        return self._username


class VKBackend(ErrBot):
    def __init__(self, config):
        super().__init__(config)
        config.MESSAGE_SIZE_LIMIT = MESSAGE_SIZE_LIMIT
        logging.getLogger('VK.bot').addFilter(VKBotFilter())

        identity = config.BOT_IDENTITY
        if identity.get('token', None):
            self.token = identity.get('token')
        else:

            self.login = identity.get('login', None)
            self.password = identity.get('password', None)
            self.token = None

        self.vk = None  # Will be initialized in serve_once
        self.bot_instance = None  # Will be set in serve_once

        compact = config.COMPACT_OUTPUT if hasattr(config, 'COMPACT_OUTPUT') else False
        enable_format('text', TEXT_CHRS, borders=not compact)
        self.md_converter = text()

    @lru_cache_ignoring_first_argument(128)
    def get_user_query(self, id_):

        try:
            # user query doesnt work with group token
            if not self.token:
                user = self.vkapi.users.get(user_ids=id_)
                # print(user)
                return user[0]
            else:
                return None
        except Exception:
            log.exception(
                "An exception occurred while trying to get user {}".format(id_)
            )
            raise

    @lru_cache_ignoring_first_argument(128)
    def get_chat_query(self, id_):
        try:
            chat = self.vkapi.messages.getChat(chat_id=id_, fields="1")
            return chat
        except Exception:
            log.exception(
                "An exception occurred while trying to get user {}".format(id_)
            )
            raise

    @lru_cache_ignoring_first_argument(128)
    def get_photo_by_album_id(self, owner_id, album_id):
        try:
            photos = self.vkapi.photos.get(owner_id=owner_id, album_id=album_id, count=1000)
            return photos
        except Exception:
            log.exception("fail")
            raise

    def init_long_polling(self, update=0):
        result = self.vkapi.messages.getLongPollServer(use_ssl=1)
        if not result:
            log.exception("Can't get Long Polling server from VK API!")
        if update == 0:
            # If this is a first initialization - we need to change a server
            self.longpoll_server = "https://" + result['server']
        if update in (0, 3):
            # If we need to initialize, or error code is 3
            # We need to get long polling key and last timestamp
            self.longpoll_key = result['key']
            self.last_ts = result['ts']
        elif update == 2:
            # If error codeis 2 - we need to get a new key
            self.longpoll_key = result['key']

        self.longpoll_values = {
            'act': 'a_check',
            'key': self.longpoll_key,
            'ts': self.last_ts,
            'wait': 20,  # Request time-out
            'mode': 2,
            'version': 1
        }

    def serve_once(self):
        log.info("Initializing connection")
        try:
            if self.token:
                self.vk_session = vk.VkApi(token=self.token)
            else:

                self.vk_session = vk.VkApi(self.login, self.password)
            self.vk_session.authorization()
            self.vkapi = self.vk_session.get_api()

            if not self.token:
                me = self.vkapi.users.get()[0]
                self.bot_identifier = VKPerson(
                    id=me["id"],
                    first_name=None,
                    last_name=None,
                    username=None
                )

        except vk.AuthorizationError as e:
            log.error("Connection failure: %s", e.message)
            return False

        log.info("Connected")
        self.reset_reconnection_count()
        self.connect_callback()
        self.init_long_polling()
        self.pollConfig = {"mode": 66, "wait": 30, "act": "a_check"}
        self.last_message_id = 0

        try:
            while True:
                try:
                    data = requests.post(self.longpoll_server, params=self.longpoll_values)
                    response = data.json()
                except ValueError:
                    continue
                failed = response.get('failed')
                if failed:
                    err_num = int(failed)
                    # Error code 1 - Timestamp needs to be updated
                    if err_num == 1:
                        self.longpoll_values['ts'] = response['ts']
                    # Error codes 2 and 3 - new Long Polling server is required
                    elif err_num in (2, 3):
                        self.init_long_polling(err_num)
                    continue
                self.longpoll_values['ts'] = response['ts']
                for update in response["updates"]:
                    # check if its real message
                    if update[0] == 4:
                        # print("got message")
                        if update[1] > self.last_message_id:
                            self.last_message_id = int(update[1])
                            log.debug(update)
                            self._handle_message(update)



        except KeyboardInterrupt:
            log.info("Interrupt received, shutting down..")
            return True
        except:
            log.exception("Error reading from VK updates stream:")
        finally:
            log.debug("Triggering disconnect callback")
            self.disconnect_callback()

    def _handle_message(self, message):

        message_instance = Message(message[6], extras={'forward_messages': message[1]})

        if message[3] > 2000000000:
            # conference chat
            room = VKRoom(id=message[3] - 2000000000, title=message[5])
            user_id = message[7].get("from", "?")
            user = self.get_user_query(user_id)
            message_instance.frm = VKMUCOccupant(
                id=user_id,
                room=room,
                first_name=user["first_name"],
                last_name=user["last_name"],
                username="test"
            )
            message_instance.to = message[3]
        else:

            # private
            user_id = str(message[3])

            user = self.get_user_query(user_id)
            if user:
                message_instance.frm = VKPerson(
                    id=str(message[3]),
                    first_name=user["first_name"],
                    last_name=user["last_name"],
                    username="test"
                )
            else:
                message_instance.frm = VKPerson(
                    id=str(message[3]),
                    first_name=None,
                    last_name=None,
                    username="test"
                )
            message_instance.to = message[3]

        log.info("[{}]: {}".format(message[3], message[6]))

        message_instance.extras["forward_messages"] = message[1]

        if message[7].get("source_act", None):
            if message[7].get("source_act", None) == "chat_invite_user":
                if int(message[7]["source_mid"]) == int(self.bot_identifier.id):
                    self.callback_room_joined(self)
        else:
            self.callback_message(message_instance)

    @rate_limited(rate_limit)  # <---- Rate Limit
    def send_message(self, mess):
        super().send_message(mess)
        body = self.md_converter.convert(mess.body)

        payload = {"peer_id": mess.to,
                   "message": body,
                   }

        if mess.extras.get("fwd_off", None) != True:
            if mess.extras.get("forward_messages", None):
                payload["forward_messages"] = mess.extras["forward_messages"]

        if mess.extras.get("attachment", None):
            payload["attachment"] = mess.extras["attachment"]

        sent_message = self.vkapi.messages.send(**payload)

    def send_reply(self, mess, text):

        mess.body = text
        self.send_message(mess)

    def change_presence(self, status: str = ONLINE, message: str = '') -> None:
        pass

    def build_identifier(self, txtrep):
        """
        Convert a textual representation into a :class:`~VKPerson` or :class:`~VKRoom`.
        """
        log.debug("building an identifier from %s" % txtrep)
        if not self._is_numeric(txtrep):
            raise ValueError("VK identifiers must be numeric")
        id_ = int(txtrep)
        if id_ < 2000000000:
            return VKPerson(id=id_)
        else:
            return VKRoom(id=id_)

    def build_reply(self, mess, text=None, private=False):
        response = self.build_message(text)
        # response.frm = self.bot_identifier
        if private:
            response.to = mess.frm
        else:
            response.to = mess.frm if mess.is_direct else mess.to
        return response

    @property
    def mode(self):
        return 'VK'

    def query_room(self, room):
        """
        Not supported on VK.

        :raises: :class:`~RoomsNotSupportedError`
        """
        raise RoomsNotSupportedError()

    def rooms(self):
        """
        Not supported on VK.

        :raises: :class:`~RoomsNotSupportedError`
        """
        raise RoomsNotSupportedError()

    def prefix_groupchat_reply(self, message, identifier):
        super().prefix_groupchat_reply(message, identifier)
        message.body = '@{0}: {1}'.format(identifier.nick, message.body)

    @staticmethod
    def _is_numeric(input_):
        """Return true if input is a number"""
        try:
            int(input_)
            return True
        except ValueError:
            return False
