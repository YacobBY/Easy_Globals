import logging
import traceback
import time
from pymemcache.client import base
from pymemcache.client import PooledClient
from pymemcache import serde
from pymemcache.client.retrying import RetryingClient
class Globals:
    # Don t put this in init, that will break with setattr
    memcached_ip = 'localhost'
    memcached_port = 11211
    # Added config client to set mem limit because pooledclient doesn't have this function yet
    config_client = base.Client((memcached_ip, memcached_port), serde=serde.pickle_serde, connect_timeout=10, no_delay=True)
    config_client.cache_memlimit(10_000) # set 10_000MB memory limit
    # config_client.disconnect_all()

    pooledClient = base.PooledClient((memcached_ip, memcached_port), serde=serde.compressed_serde,
                                                 connect_timeout=10,pool_idle_timeout=10, no_delay=True)

    # Wrap into retry client
    memcached_globals_client = RetryingClient(pooledClient,attempts=3)

    def reset_all_globals(self):
        self.memcached_globals_client.flush_all()

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
        except Exception as E:
            logging.log(level=40, msg=f'Error setting global: {E}')

    def __getattr__(self, key):
        try:
            return self.memcached_globals_client.get(key)
        except ConnectionRefusedError:
            self.log_error_message_globals(ConnectionRefusedError)
            return
        except Exception as E:
            for i in range(20):
                try:
                    return self.memcached_globals_client.get(key)
                except:
                    continue

        # except Exception as E:
        #     print(f'Memcache error :{E} retrying')
        #     return self.memcached_globals_client.get(key)



    #
    def __getitem__(self, key):
        return getattr(self, key)

    def __setitem__(self, key, value):
        return setattr(self, key, value)