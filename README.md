# Introduction
 EasyGlobals is an easy to use fast database to share variables between Python processes.

It uses Memcached in the background to share full objects between Python processes. Inspired by many closed source languages which have an easy way to share "global" variables between processes while retaining pythonic syntax. 

No need for Multiprocessing Manager dicts, queues and other complex data pipelines in your program anymore. Simply make a globals object called g and start 


# Installation
Install Memcached which acts as the server. installation with Apt is availible on Ubuntu. Different operating systems haven't been tested yet but installation guides with Windows can be found online
```
sudo apt install memcached
sudo systemctl start memcached
```

Now you can install the easyglobals library:
```
pip install easyglobals
```


# Usage
After installation following the instructions down below, try running the following example
```
import EasyGlobals
g = EasyGlobals.Globals()

g.test1 = 4
g.test2 = 'hello world'
g.test3 = {'dictkey1': globals.test1, 'dictkey2': globals.test2} #  Dict

print(g.test1)
print(g.test2)
print(g.test3)
```
- Try to write to a variable in only one process. Reading the variable can be done from anywhere. Writing on multiple processes is not restricted, but you will need to deal with potential race conditions. So prepare for coding headaches if you do.


# Limitations:
- Any variable type that can be pickled should work. E.g. Numpy arrays, OpenCV images.
- Directly getting and setting values from nested objects such as dicts or classes in the Globals can be problematic as the values are in binary format (pickled) while they are in memcached.
- It's currently best to retrieve the an object from globals, modify it locally, and then store the entire object in globals again.
- If you're already using Memcached and your variable keys have the same name they will be overwritten
- All variables will stay stored in Memcached even when your program stops. Restart memached to clear them, for example by running "service memcached restart" in the terminal.

# Speed 🚀
Tested on a Ryzen 5900X
- 100.000 writes/reads per second on for small variables
- 1.000 writes/reads per second for big variables such as 4K OpenCV images. 


- This is around 200-400 times slower than using normal Python local variables. For most programs this won't matter but keep in mind that any variable in direct process memory will always be faster than having to pickle it and send it to Memcached.
- Redis and SQLite have been tested as potential back-ends but were too slow.
- Memcached was the fastest as it is a dedicated key-value DB.
- Redis is more designed for distributed computing and storing a huge amount of variables.
- SQLite cannot share variables between processes while running in-memory.

# Multiprocessing example:
```
from EasyGlobals import EasyGlobals
import multiprocessing

def write_to_globals():
    g = EasyGlobals.Globals()
    for i in range(1_000):
        g.testvar = i

def retrieve_from_globals(process_id):
    g = EasyGlobals.Globals()
    for i in range(10):
        result = g.testvar
        print(f'Process {process_id}, read: {result}')


print('Start writing process')
write_process = multiprocessing.Process(target=write_to_globals)
write_process.start()

print('Start reading with 3 simultaneous processes')
processlist = []
for i in range(5):
    processlist.append(multiprocessing.Process(target=retrieve_from_globals, args=(i,)))
    processlist[i].start()

for process in processlist:
    process.join()
print('Done reading')
write_process.join()
print('Done writing')

```

