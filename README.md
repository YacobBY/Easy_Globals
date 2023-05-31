# Introduction
 EasyGlobals is an easy to use variable sharing object for Python that can quickly share complex objects between processes.

It uses Memcached in the background to share full objects and other complex variables between Python processes. Inspired by many closed source languages which have an easy way to share "global" variables between processes while retaining pythonic syntax.

# Usage
```
import EasyGlobals
globals = EasyGlobals.Globals()

globals.test1 = 4
globals.test2 = 'hello world'
globals.test3 = {'dictkey1': globals.test1, 'dictkey2': globals.test2} #  Dict

print(globals.test1)
print(globals.test2)
print(globals.test3)
```


# Installation
Install Memcached which acts as the server. Easy install through Apt is availible on Ubuntu. Different operating systems haven't been tested yet
```
sudo apt install memcached
sudo systemctl start memcached
```

Now you can install the easyglobals library:
```
pip install easyglobals
```

- Keep in mind that locks are not implemented here, meaning that race conditions can happen if two processes are writing to the same variable at the same time. For example when they're both incrementing the same value in a loop this can cause unwanted behavior.

- To prevent this, an easy solution is to write to a variable in only one process. Reading can be done in as many processes as desired.

# Limitations:
- Any variable type that can be pickled should work. E.g. Numpy arrays, OpenCV images.
- Directly Getting and setting values from nested objects such as dicts or classes in the Globals can be problematic as the values are in binary format (pickled) while they are in memcached.
- It's currently best to retrieve the an object from globals, modify it locally, and then store the entire object in globals again
- If you're already using Memcached and your variable keys have the same name they will be overwritten
- All variables will stay stored in Memcached even when your program stops. Restart memached to clear them, for example by running "service memcached restart" in the terminal.

# Speed 🚀
Tested on a Ryzen 5900X
- 100_000 writes/reads per second on for small variables
- 1_000 writes/reads per secont for big variables such as 4K OpenCV images. 


- This is around 200-400 times slower than using normal Python local variables. For most programs this won't matter but keep in mind that any variable in direct process memory will always be faster than having to pickle it and send it to Memcached.

- Redis and SQLite have been tested as potential back-ends.
- Memcached was the fastest as it is a dedicated key-value DB.
- Redis is more designed for distributed computing and storing a huge amount of variables.
- SQLite cannot share variables between processes while running in-memory.
