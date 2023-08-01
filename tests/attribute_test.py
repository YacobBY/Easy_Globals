import time

from pymemcache.client import base
from pymemcache import serde
class Globals:

    # Don t put this in init, that will break with setattr
    client = base.Client(('localhost', 11211), serde=serde.pickle_serde)

    def __setattr__(self, key, value):
        self.client.set(key, value)
    def __getattr__(self, key):
        return self.client.get(key)


    # def __getattribute__(self, key):
    #     # print(f'Key: {key}')

        # except AttributeError:
        #     # If a variable doesn't exist in the class yet, check if it is in DB first.
        #     # If so create it as  local variable too.
        #     object.__setattr__(self, 'RetrievalValueTest', self.client.get(key))
        #     if   self.RetrievalValueTest is not None:
        #         # print('added key')
        #         object.__setattr__(self, key, 'in_db')
        #         return  self.RetrievalValueTest
        #     else:
        #         print(f'ERROR: Key --> {key} <-- not found in Memcahced Globals!')
        #         raise AttributeError

g = Globals()
g.a = 5

print(g.a)
time.sleep(0.5)
print(g.b)