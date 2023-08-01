import logging
import traceback
import time
from pymemcache.client import base
from pymemcache import serde

class Globals:
    # Don t put this in init, that will break with setattr
    memcached_ip = 'localhost'
    memcached_port = 11211
    memcached_globals_client = base.Client((memcached_ip, memcached_port), serde=serde.pickle_serde, connect_timeout=10, no_delay=True)
    memcached_globals_client.cache_memlimit(8000)


    def log_error_message_globals(self, exception):
        if exception == ConnectionRefusedError:
            exception_info = traceback.format_exc(limit=1)
            logging.log(level=40 , msg=exception_info)
            logging.log(level = 40, msg =
                 (f'{30*"*"} Error: EasyGlobals failed to connect to Memcached. {30*"*"}\n'
                  f'-       Is Memcahed installed? https://github.com/YacobBY/Easy_Globals \n'
                  f'-       Is the Memcached server running on port {self.memcached_port}?'))

    def __setattr__(self, key, value):
        try:
            self.memcached_globals_client.set(key, value)
        except ConnectionRefusedError:
            self.log_error_message_globals(ConnectionRefusedError)

    def __getattr__(self, key):
        try:
            return self.memcached_globals_client.get(key)
        except ConnectionRefusedError:
            self.log_error_message_globals(ConnectionRefusedError)

    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        return setattr(self, key, value)