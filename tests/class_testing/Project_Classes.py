# Currently when importing this it will reset all variables so don't import in middle of code
import time

# Don't import anything else here
class LaserAttributes:
    def __init__(self):
        self.distance = 0 # distance in milimetres; float.
        self.error = True
        self.error_message = ''  # usage tbd
        # self.laser_sending_hz = 2000
        self.in_range = False
        self.in_range_active = False
        self.last_in_range_time = 0
        self.last_update_time = 0

