import socket
import json
import time
from datetime import datetime
import threading
import sys, os
import math
import uuid
from time import sleep
import collections
from blinker import signal
import geomag

import numpy as np

import tzlocal
import queue

from device.config import Config
from device.seestar_util import Util

from collections import OrderedDict

class FixedSizeOrderedDict(OrderedDict):
    def __init__(self, *args, maxsize=None, **kwargs):
        self.maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if self.maxsize is not None and len(self) >= self.maxsize:
            self.popitem(last=False)  # Remove the oldest item
        super().__setitem__(key, value)



class Seestar:
    def __new__(cls, *args, **kwargs):
        # print("Create a new instance of Seestar.")
        return super().__new__(cls)

    # <ip_address> <port> <device name> <device num>
    def __init__(self, logger, host, port, device_name, device_num, is_EQ_mode, is_debug=False):
        logger.info(
            f"Initialize the new instance of Seestar: {host}:{port}, name:{device_name}, num:{device_num}, is_EQ_mode:{is_EQ_mode}, is_debug:{is_debug}")

        self.host = host
        self.port = port
        self.device_name = device_name
        self.device_num = device_num
        self.cmdid = 10000
        self.site_latitude = Config.init_lat
        self.site_longitude = Config.init_long
        self.site_elevation = 0
        self.ra = 0.0
        self.dec = 0.0
        self.is_watch_events = False  # Tracks if device has been started even if it never connected
        self.s = None
        self.get_msg_thread = None
        self.heartbeat_msg_thread = None
        self.is_debug = is_debug
        self.response_dict = FixedSizeOrderedDict(max=100)
        self.logger = logger
        self.is_connected = False
        self.is_slewing = False
        self.target_dec = 0
        self.target_ra = 0
        self.utcdate = time.time()

        self.mosaic_thread = None
        self.scheduler_thread = None
        self.schedule = {}
        self.schedule['schedule_id'] = str(uuid.uuid4())
        self.schedule['list'] = collections.deque()
        self.schedule['state'] = "stopped"
        self.schedule['current_item_id'] = ""       # uuid of the current/active item in the schedule list
        self.schedule["item_number"] = 0             # order number of the schedule_item in the schedule list
        self.is_cur_scheduler_item_working = False
        self.is_below_horizon_goto_method = False
        
        self.event_state = {}
        self.update_scheduler_state_obj({}, result=0)

        self.cur_solve_RA = -9999.0  #
        self.cur_solve_Dec = -9999.0
        self.connect_count = 0
        self.below_horizon_dec_offset = 0  # we will use this to work around below horizon. This value will ve used to fool Seestar's star map
        self.safe_dec_for_offset = 10.0     # declination angle in degrees as the lower limit for dec values before below_horizon logic kicks in
        self.custom_goto_state = "stopped" # for custom goto logic used by below_horizon, using auto centering algorithm
        self.view_state = {}

        # self.event_queue = queue.Queue()
        self.event_queue = collections.deque(maxlen=20)
        self.eventbus = signal(f'{self.device_name}.eventbus')
        self.is_EQ_mode = is_EQ_mode

    # scheduler state example: {"state":"working", "schedule_id":"abcdefg", 
    #       "result":0, "error":"dummy error",
    #       "cur_schedule_item":{   "type":"mosaic", "schedule_item_GUID":"abcde", "state":"working",
    #                               "stack_status":{"target_name":"test_target", "stack_count": 23, "rejected_count": 2},
    #                               "item_elapsed_time_s":123, "item_remaining_time":-1}
    #       }




    def __repr__(self) -> str:
        return f"{type(self).__name__}(host={self.host}, port={self.port})"

    def update_scheduler_state_obj(self, item_state, result = 0):
        self.event_state["scheduler"]  = {"schedule_id": self.schedule['schedule_id'], "state":self.schedule['state'], 
                                            "item_number": self.schedule["item_number"], "cur_scheduler_item": item_state , "result":result}
    
    def heartbeat(self):  # I noticed a lot of pairs of test_connection followed by a get if nothing was going on
        #    json_message("test_connection")
        self.json_message("scope_get_equ_coord", id=420)

    def send_message(self, data):
        try:
            if self.s == None:
                self.logger.warn("socket not initialized!")
                time.sleep(3)
                return False
            # todo : don't send if not connected or socket is null?
            self.s.sendall(data.encode())  # TODO: would utf-8 or unicode_escaped help here
            return True
        except socket.timeout:
            return False
        except socket.error as e:
            # Don't bother trying to recover if watch events is False
            self.logger.debug(f"Send socket error: {e}")
            self.disconnect()
            if self.is_watch_events and self.reconnect():
                return self.send_message(data)
            return False
        except:
            self.logger.error(f"General error trying to send message: ", data)
            return False

    def socket_force_close(self):
        if self.s:
            try:
                self.s.close()
                self.s = None
            except:
                pass

    def disconnect(self):
        # Disconnect tries to clean up socket if it exists
        self.is_connected = False
        self.socket_force_close()

    def reconnect(self):
        if self.is_connected:
            return True

        try:
            self.logger.info(f"RECONNECTING {self.device_name}")

            self.disconnect()

            # note: the below isn't thread safe!  (Reconnect can be called from different threads.)
            self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.s.settimeout(Config.timeout)
            self.s.connect((self.host, self.port))
            # self.s.settimeout(None)
            self.is_connected = True
            return True
        except socket.error as e:
            # Let's just delay a fraction of a second to avoid reconnecting too quickly
            self.is_connected = False
            sleep(1)
            return False
        except Exception as ex:
            self.is_connected = False
            sleep(1)
            return False

    def get_socket_msg(self):
        try:
            if self.s == None:
                self.logger.warn("socket not initialized!")
                time.sleep(3)
                return None
            data = self.s.recv(1024 * 60)  # comet data is >50kb
        except socket.timeout:
            self.logger.warn("Socket timeout")
            return None
        except socket.error as e:
            # todo : if general socket error, close socket, and kick off reconnect?
            # todo : no route to host...
            self.logger.debug(f"Read socket error: {e}")
            # todo : handle message failure
            self.disconnect()
            if self.is_watch_events and self.reconnect():
                return self.get_socket_msg()
            return None
        
        data = data.decode("utf-8")
        if len(data) == 0:
            return None

        return data

    def update_equ_coord(self, parsed_data):
        if parsed_data['method'] == "scope_get_equ_coord" and 'result' in parsed_data:
            data_result = parsed_data['result']
            self.ra = float(data_result['ra'])
            self.dec = float(data_result['dec'] - self.below_horizon_dec_offset)

    def update_view_state(self, parsed_data):
        if parsed_data['method'] == "get_view_state" and 'result' in parsed_data:
            view = parsed_data['result'].get('View')
            if view:
                self.view_state = view
            #else:
            #    self.view_state = {}

    def heartbeat_message_thread_fn(self):
        while self.is_watch_events:
            threading.current_thread().last_run = datetime.now()

            if not self.is_connected and not self.reconnect():
                sleep(5)
                continue

            self.heartbeat()
            time.sleep(3)

    def receive_message_thread_fn(self):
        msg_remainder = ""
        while self.is_watch_events:
            threading.current_thread().last_run = datetime.now()
            # print("checking for msg")
            data = self.get_socket_msg()
            if data:
                msg_remainder += data
                first_index = msg_remainder.find("\r\n")

                while first_index >= 0:
                    first_msg = msg_remainder[0:first_index]
                    msg_remainder = msg_remainder[first_index + 2:]
                    try:
                        parsed_data = json.loads(first_msg)
                    except Exception as e:
                        self.logger.exception(e)
                        # We just bail for now...
                        break

                    if 'jsonrpc' in parsed_data:
                        # {"jsonrpc":"2.0","Timestamp":"9507.244805160","method":"scope_get_equ_coord","result":{"ra":17.093056,"dec":34.349722},"code":0,"id":83}
                        if parsed_data["method"] == "scope_get_equ_coord":
                            self.logger.debug(f'{parsed_data}')
                            self.update_equ_coord(parsed_data)
                        else:
                            self.logger.debug(f'{parsed_data}')
                        if parsed_data["method"] == "get_view_state":
                            self.update_view_state(parsed_data)
                        # keep a running queue of last 100 responses for sync call results
                        self.response_dict[parsed_data["id"]] = parsed_data

                    elif 'Event' in parsed_data:
                        # add parsed_data
                        self.event_queue.append(parsed_data)
                        self.eventbus.send(parsed_data)

                        # xxx: make this a common method....
                        if Config.log_events_in_info:
                            self.logger.info(f'received : {parsed_data}')
                        else:
                            self.logger.debug(f'received : {parsed_data}')
                        event_name = parsed_data['Event']
                        self.event_state[event_name] = parsed_data

                        # {'Event': 'PlateSolve', 'Timestamp': '15221.315064872', 'page': 'preview', 'tag': 'Exposure-AutoGoto', 'ac_count': 1, 'state': 'complete', 'result': {'ra_dec': [3.252308, 41.867462], 'fov': [0.712052, 1.265553], 'focal_len': 252.081757, 'angle': -175.841003, 'image_id': 1161, 'star_number': 884, 'duration_ms': 13185}}
                        # {'Event': 'PlateSolve', 'Timestamp': '21778.539366227', 'state': 'fail', 'error': 'solve failed', 'code': 251, 'lapse_ms': 30985, 'route': []}

                        if event_name == 'PlateSolve':
                            if 'result' in parsed_data and 'ra_dec' in parsed_data['result']:
                                self.logger.info("Plate Solve Succeeded")
                                self.cur_solve_RA = parsed_data['result']['ra_dec'][0]
                                self.cur_solve_Dec = parsed_data['result']['ra_dec'][1]
                            elif parsed_data['state'] == 'fail':
                                self.logger.info("Plate Solve Failed")
                                self.cur_solve_RA = 0
                                self.cur_solve_Dec = 0
                        #else:
                        #    self.logger.debug(f"Received event {event_name} : {data}")

                    first_index = msg_remainder.find("\r\n")
            time.sleep(0.1)

    def json_message(self, instruction, **kwargs):
        data = {"id": self.cmdid, "method": instruction, **kwargs}
        self.cmdid += 1
        json_data = json.dumps(data)
        if instruction == 'scope_get_equ_coord':
            self.logger.debug(f'sending: {json_data}')
        else:
            self.logger.debug(f'sending: {json_data}')
        self.send_message(json_data + "\r\n")

    def send_message_param(self, data):
        cur_cmdid = data.get('id') or self.cmdid
        data['id'] = cur_cmdid
        self.cmdid += 1 # can this overflow?  not in JSON...
        json_data = json.dumps(data)
        if 'method' in data and data['method'] == 'scope_get_equ_coord':
            self.logger.debug(f'sending: {json_data}')
        else:
            self.logger.debug(f'sending: {json_data}')

        self.send_message(json_data + "\r\n")
        return cur_cmdid

    def shut_down_thread(self, data):
        self.play_sound(13)
        result = self.reset_below_horizon_dec_offset()
        response = self.send_message_param_sync({"method":"scope_park"})
        self.logger.info(f"Parking before shutdown...{response}")
        self.event_state["ScopeHome"] = {"state":"working"}
        result = self.wait_end_op("ScopeHome")
        self.logger.info(f"Parking result...{result}")            
        self.logger.info(f"About to send shutdown or reboot command to Seestar...{response}")
        cur_cmdid = self.send_message_param(data)

    def send_message_param_sync(self, data):
        if data['method'] == 'pi_shutdown' or data['method'] == 'pi_reboot':
            threading.Thread(name=f"shutdown-thread:{self.device_name}", target=lambda: self.shut_down_thread(data)).start()
            return {'method': data['method'], 'result': "Sent command async for these types of commands." }
        else:
            cur_cmdid = self.send_message_param(data)

        start = time.time()
        last_slow = start
        while cur_cmdid not in self.response_dict:
            now = time.time()
            if now - last_slow > 2:
                elapsed = now - start
                last_slow = now
                if elapsed > 10:
                    self.logger.error(f'Failed to wait for message response.  {elapsed} seconds. {cur_cmdid=} {data=}')
                    data['result'] = "Error: Exceeded alloted wait time for result"
                    return data
                else:
                    self.logger.warn(f'SLOW message response.  {elapsed} seconds. {cur_cmdid=} {data=}')
            time.sleep(0.5)
        self.logger.debug(f'response is {self.response_dict[cur_cmdid]}')
        return self.response_dict[cur_cmdid]

    def get_event_state(self, params=None):
        self.event_state["scheduler"]["state"] = self.schedule["state"]
        if params != None and 'event_name' in params:
            event_name = params['event_name']
            if event_name in self.event_state:
                result = self.event_state[event_name]
            else:
                result = {}
        else:
            result = self.event_state
        return self.json_result("get_event_state", 0, result)

        
    def set_setting(self, x_stack_l, x_continuous, d_pix, d_interval, d_enable, l_enhance):
        # TODO:
        #   heater_enable failed. 
        #   lenhace should be by itself as it moves the wheel and thus need to wait a bit
        #    data = {"id":cmdid, "method":"set_setting", "params":{"exp_ms":{"stack_l":x_stack_l,"continuous":x_continuous}, "stack_dither":{"pix":d_pix,"interval":d_interval,"enable":d_enable}, "stack_lenhance":l_enhance, "heater_enable":heater_enable}}
        data = {"method": "set_setting", "params": {"exp_ms": {"stack_l": x_stack_l, "continuous": x_continuous},
                                                    "stack_dither": {"pix": d_pix, "interval": d_interval,
                                                                     "enable": d_enable}, "stack_lenhance": l_enhance}}
        result = self.send_message_param_sync(data)
        time.sleep(2)  # to wait for filter change
        return result

    def stop_goto_target(self):
        if self.is_goto():
            if self.below_horizon_dec_offset == 0:
                return self.stop_slew()
            else:
                # need to stop the custom goto routine for below horizon
                self.custom_goto_state = "stopping"
                return "Stop requested."
        return "goto stopped already: no action taken"

    def mark_goto_status_as_start(self):
        if self.is_below_horizon_goto_method:
            self.event_state["ScopeGoto"] = {"state":"start"}
        else:
            self.event_state["AutoGoto"] = {"state":"start"}

    def mark_goto_status_as_stopped(self):
        if self.is_below_horizon_goto_method:
            self.event_state["ScopeGoto"] = {"state":"stopped"}
        else:
            self.event_state["AutoGoto"] = {"state":"stopped"}

    def is_goto(self):
        try:
            if self.is_below_horizon_goto_method:
                event_watch = "ScopeGoto"
            else:
                event_watch = "AutoGoto"
            self.logger.debug(f"{event_watch} status is {self.event_state[event_watch]["state"]}")
            return self.event_state[event_watch]["state"] == "working" or self.event_state[event_watch]["state"] == "start"
        except:
            return False

    def is_goto_completed_ok(self):
        try:
            if self.is_below_horizon_goto_method:
                return self.event_state["ScopeGoto"]["state"] == "complete"
            else:
                return self.event_state["AutoGoto"]["state"] == "complete"
        except:
            return False
                
    def goto_target(self, params):
        if self.is_goto():
            self.logger.info("Failed to goto target: mount is in goto routine.")
            return {"result":"Failed to goto target: mount is in goto routine."}
        self.mark_goto_status_as_start()
        threading.Thread(name=f"goto-target-thread:{self.device_name}", target=lambda: self.goto_target_thread(params)).start()
        return {"result":0}

    def goto_target_thread(self, params):

        is_j2000 = params['is_j2000']
        in_ra = params['ra']
        in_dec = params['dec']
        parsed_coord = Util.parse_coordinate(is_j2000, in_ra, in_dec)
        in_ra = parsed_coord.ra.hour
        in_dec = parsed_coord.dec.deg
        target_name = params.get("target_name", "unknown")
        self.logger.info("%s: going to target... %s %s %s, with initial dec offset %s", self.device_name, target_name, in_ra,
                         in_dec, self.below_horizon_dec_offset)        
        result = True
        if self.is_EQ_mode:
            if in_dec < -Config.init_lat:
                msg = f"Failed. You tried to geto to a target [ {in_ra}, {in_dec} ] that seems to be too low for your location at lat={Config.init_lat}"
                self.logger.warn(msg)
                self.mark_goto_status_as_stopped()
                return
            
            safe_dec_offset = -in_dec+self.safe_dec_for_offset
            if self.below_horizon_dec_offset > 0 and in_dec > self.safe_dec_for_offset:
                result = self.reset_below_horizon_dec_offset()
            elif safe_dec_offset > self.below_horizon_dec_offset:
                result = self.set_below_horizon_dec_offset(safe_dec_offset, in_dec)

            if result != True:
                self.logger.warn("Failed to set or reset horizontal dec offset. Goto will not proceed.")
                self.mark_goto_status_as_stopped()
                return
        
        if self.below_horizon_dec_offset == 0:
            self.is_below_horizon_goto_method = False
            data = {}
            data['method'] = 'iscope_start_view'
            params = {}
            params['mode'] = 'star'
            ra_dec = [in_ra, in_dec]
            params['target_ra_dec'] = ra_dec
            params['target_name'] = target_name
            params['lp_filter'] = False
            data['params'] = params
            self.send_message_param_sync(data)
            return
        else:
            # do the same, but when trying to center on target, need to implement ourselves to platesolve correctly to compensate for the dec offset
            self.goto_target_with_dec_offset_async(target_name, in_ra, in_dec)
            return

    # {"method":"scope_goto","params":[1.2345,75.0]}
    def _slew_to_ra_dec(self, params):
        in_ra = params[0]
        in_dec = params[1]
        self.logger.info(f"slew to {in_ra}, {in_dec} with dec_offset of {self.below_horizon_dec_offset}")
        data = {}
        data['method'] = 'scope_goto'
        params = [in_ra, in_dec + self.below_horizon_dec_offset]
        data['params'] = params
        result = self.send_message_param_sync(data)
        if 'error' in result:
            self.logger.warn("Error while trying to move: %s", result)
            return False
        else:
            self.is_below_horizon_goto_method = True

        return self.wait_end_op("goto_target")

    def set_below_horizon_dec_offset(self, offset, target_dec):
        if offset <= 0:
            msg = f"Failed: offset must be greater or equal to 0: {offset}"
            self.logger.warn(msg)
            return False  
        
        if self.below_horizon_dec_offset == 0 and offset > 90-self.site_latitude:
            msg = f"Cannot set dec offset too high: {offset}. It should be less than 90 - <your lattitude>."
            self.logger.warn(msg)
            return False         
             
        # we cannot fake the position too high, so we may need to move the scope down first
        if self.dec + offset > 70.0:
            if not self.reset_below_horizon_dec_offset():
                self.logger.warn(f"Failed to reset dec offset before applying a large offset  of {self.dec + offset}")
                return False
            offset = -target_dec + self.safe_dec_for_offset
            #time.sleep(5)

        old_dec = self.dec
        self.below_horizon_dec_offset = offset
        result = self._sync_target([self.ra, old_dec])
        if 'error' in result:
            self.below_horizon_dec_offset = 0
            self._sync_target([self.ra, old_dec])
            self.logger.warn(result)
            self.logger.warn("Failed to set dec offset. Move the mount up first?")
            return False
        return True

    def reset_below_horizon_dec_offset(self):
        if not self.is_EQ_mode:
            return
        
        old_ra = self.ra
        old_dec = self.dec
        old_offset = self.below_horizon_dec_offset

        self.logger.info(f"starting to reset dec offset from [{old_ra}, {old_dec}], with below_horizon_dec_offset of {self.below_horizon_dec_offset}")
        new_dec = self.safe_dec_for_offset       # 5 degree above celestrial horizon
        self.logger.info(f"slew to {old_ra}, {new_dec}")
        result = self._slew_to_ra_dec([old_ra, new_dec]) # dec was already offset
        if result == True:
            #time.sleep(10)
            self.below_horizon_dec_offset = 0
            self.logger.info(f"syncing to {old_ra}, {new_dec}")
            response = self._sync_target([old_ra, new_dec])
            self.logger.info(f"response from synC: {response}")
            if "error" in response:
                return False
            else:
                time.sleep(2)
                return True
        else:
            self.logger.error("Failed to move back from the offset!")
            return False


    def sync_target(self, params):
        if self.schedule['state'] != "stopped" or self.schedule['state'] != "complete":
            msg = f"Cannot sync target while scheduler is active: {self.schedule['state']}"
            self.logger.warn(msg)
            return msg
        else:
            return self._sync_target(params)

    def _sync_target(self, params):
        in_ra = params[0]
        in_dec = params[1]
        self.logger.info("%s: sync to target... %s %s with dec_offset of %s", self.device_name, in_ra, in_dec,
                         self.below_horizon_dec_offset)
        data = {}
        data['method'] = 'scope_sync'
        data['params'] = [in_ra, in_dec + self.below_horizon_dec_offset]
        result = self.send_message_param_sync(data)
        if 'error' in result:
            self.logger.info(f"Failed to sync: {result}")
        else:
            sleep(2)
        return result

    def stop_slew(self):
        self.logger.info("%s: stopping slew...", self.device_name)
        data = {}
        data['method'] = 'iscope_stop_view'
        params = {}
        params['stage'] = 'AutoGoto'
        data['params'] = params
        return self.send_message_param_sync(data)
        # TODO: need to handle this for our custom goto for below horizon too

    # {"method":"scope_speed_move","params":{"speed":4000,"angle":270,"dur_sec":10}}
    def move_scope(self, in_angle, in_speed, in_dur=3):
        self.logger.debug("%s: moving slew angle: %s, speed: %s, dur: %s", self.device_name, in_angle, in_speed, in_dur)
        if self.is_goto():
            self.logger.warn("Failed to move scope: mount is in goto routine.")
            return False
        data = {}
        data['method'] = 'scope_speed_move'
        params = {}
        params['speed'] = in_speed
        params['angle'] = in_angle
        params['dur_sec'] = in_dur
        data['params'] = params
        self.send_message_param_sync(data)
        return True

    def start_auto_focus(self):
        self.logger.info("start auto focus...")
        result = self.send_message_param_sync({"method": "start_auto_focuse"})
        if 'error' in result:
            self.logger.error("Faild to start auto focus: %s", result)
            return False
        return True

    def try_auto_focus(self, try_count):
        self.logger.info("trying auto_focus...")
        focus_count = 0
        result = False
        self.event_state["AutoFocus"] = {"state":"working"}
        while focus_count < try_count:
            focus_count += 1
            self.logger.info("%s: focusing try %s of %s...", self.device_name, str(focus_count), str(try_count))
            if focus_count > 1:
                time.sleep(5)
            if self.start_auto_focus():
                result = self.wait_end_op("AutoFocus")
                if result == True:
                    break
        # give extra time to settle focuser
        time.sleep(2)
        self.logger.info("auto_focus completed")
        return result

    def start_3PPA(self):
        self.logger.info("start 3 point polar alignment...")
        result = self.send_message_param_sync({"method": "start_polar_align"})
        if 'error' in result:
            self.logger.error("Faild to start polar alignment: %s", result)
            return False
        return True
    
    def try_3PPA(self, try_count):
        self.logger.info("trying 3PPA...")
        self.is_below_horizon_goto_method = False
        cur_count = 0
        result = False
        self.event_state["3PPA"] = {"state":"working"}
        while cur_count < try_count:
            cur_count += 1
            self.logger.info("%s: 3PPA try %s of %s...", self.device_name, str(cur_count), str(try_count))
            if cur_count > 1:
                time.sleep(5)

            #todo need to check if there was a previous failed 3PPA. If so, need to stack current spot instead!
#            response = self.send_message_param_sync({"method":"iscope_get_app_state"})
#            response = response["result"]
#            if "3PPA" not in response or ("3PPA" in response and response["3PPA"]["state"] == "fail"):
            response = self.send_message_param_sync({"method":"get_device_state"})
            self.logger.info(f"get 3PPA state to determine how to proceede: {response}")

            response = response["result"]["setting"]
            is_3PPA = True
            if "offset_deg_3ppa" not in response:
                result = self.start_stack({"restart":True, "gain": Config.init_gain})
                is_3PPA = False
            else:
                result = self.start_3PPA()
            if result == True:
                time.sleep(1)
                result = False
                while True:
                    if "3PPA" in self.event_state:
                        event_state = self.event_state["3PPA"]
                        if "state" in event_state and (event_state["state"] == "fail"):
                            self.logger.info(f"3PPA failed: {event_state}.")
                            if not is_3PPA:
                                response = self.send_message_param_sync({"method":"iscope_stop_view","params":{"stage":"AutoGoto"}})
                                self.logger.info(response)
                            result = False
                            break
                        elif "percent" in event_state:
                            if event_state["percent"] > 99.9:
                                self.logger.info("3PPA reached 100%. Will stop return to origin now.")
                                if is_3PPA:
                                    response = self.send_message_param_sync({"method":"stop_polar_align"})
                                else:
                                    response = self.send_message_param_sync({"method":"iscope_stop_view","params":{"stage":"AutoGoto"}})
                                self.logger.info(response)
                                result = True
                                break
                        elif "state" in event_state and (event_state["state"] == "cancel"):
                            self.logger.info("Should not found a cancel state for 3PPA since we explicitly cancel only when we past 100% plate solve")
                            result = False
                            break
                    time.sleep(1)
                if result == True:
                    break
        # give extra time to settle focuser
        time.sleep(2)
        self.logger.info(f"3PPA done with result {result}")
        return result

    def try_dark_frame(self):
        self.logger.info("start dark frame measurement...")
        self.event_state["DarkLibrary"] = {"state":"working"}
        result = self.send_message_param_sync({"method": "start_create_dark"})
        if 'error' in result:
            self.logger.error("Faild to start create darks: %s", result)
            return False
        response = self.send_message_param_sync({"method": "set_control_value", "params": ["gain", Config.init_gain]})
        self.logger.info(f"dark frame measurement setting gain response: {response}")
        result = self.wait_end_op("DarkLibrary")

        if result == True:
            response = self.send_message_param_sync({"method":"iscope_stop_view","params":{"stage":"Stack"}})
            self.logger.info(f"Response from stop stack after dark frame measurement: {response}")
        else:
            self.logger.warn("Create dark frame data failed.")
        return result
    
    def stop_stack(self):
        self.logger.info("%s: stop stacking...", self.device_name)
        data = {}
        data['method'] = 'iscope_stop_view'
        params = {}
        params['stage'] = 'Stack'
        data['params'] = params
        return self.send_message_param_sync(data)

    def play_sound(self, in_sound_id: int):
        self.logger.info("%s: playing sound...", self.device_name)
        req = {}
        req['method'] = 'play_sound'
        params = {}
        params['num'] = in_sound_id
        req['params'] = params
        result = self.send_message_param_sync(req)
        time.sleep(1)
        return result

    def apply_rotation(self, matrix, degrees):
        # Convert degrees to radians
        radians = math.radians(degrees)
        
        # Define the rotation matrix
        rotation_matrix = np.array([[math.cos(radians), -math.sin(radians)],
                                    [math.sin(radians), math.cos(radians)]])
        
        # Multiply the original matrix by the rotation matrix
        rotated_matrix = np.dot(rotation_matrix, matrix)
        
        return rotated_matrix


    def adjust_mag_declination(self, params):
        adjust_mag_dec = params.get("adjust_mag_dec", False)
        fudge_angle = params.get("fudge_angle", 0.0)
        self.logger.info(f"adjusting device's compass bearing to account for the magnetic declination at device's position. Adjust:{adjust_mag_dec}, Fudge Angle: {fudge_angle}")
        response = self.send_message_param_sync({ "method": "get_device_state",  "params": {"keys":["location_lon_lat"]}})
        result = response["result"]
        loc = result["location_lon_lat"]

        response = self.send_message_param_sync({"method":"get_sensor_calibration"})
        compass_data = response["result"]["compassSensor"]
        x11 = compass_data["x11"]
        y11 = compass_data["y11"]
        x12 = compass_data["x12"]
        y12 = compass_data["y12"]

        total_angle = fudge_angle
        if adjust_mag_dec:
            mag_dec = geomag.declination(loc[1], loc[0])
            self.logger.info(f"mag declination for {loc[1]}, {loc[0]} is {mag_dec} degrees")
            total_angle += mag_dec
        
        # Convert the 2x2 matrix into a set of points (pairs of coordinates)
        # We treat each column of the matrix as a point (x, y)
        in_matrix = np.array([[x11, x12],  # First column: (x1, y1)
                        [y11, y12]]) # Second column: (x2, y2)

        out_matrix = self.apply_rotation(in_matrix, total_angle)
        
        # Convert the rotated points back into matrix form
        x11 = out_matrix[0, 0]
        y11 = out_matrix[1, 0]
        x12 = out_matrix[0, 1]
        y12 = out_matrix[1, 1]

        params = {"compassSensor": {"x": compass_data["x"], "y": compass_data["y"], "z": compass_data["z"], "x11": x11, "x12": x12, "y11": y11, "y12": y12}}
        self.logger.info(f"sending adjusted compass sensor data:", params)
        response = self.send_message_param_sync({"method":"set_sensor_calibration", "params": params})
        result_text = f"Adjusted compass calibration to offset by total of {total_angle} degrees."
        self.logger.info(result_text)
        response["result"] = result_text
        return response


    # {"target_name":"test_target","ra":1.234, "dec":-12.34}
    # take into account self.below_horizon_dec_offset for platesolving, using low level move and custom plate solving logic
    def goto_target_with_dec_offset_async(self, target_name, in_ra, in_dec):
        # first, go to position (ra, cur_dec)
        if in_ra < 0:
            target_ra = self.ra
            target_dec = self.dec
        else:
            target_ra = in_ra
            target_dec = in_dec
        self.logger.info("trying to go with explicit dec offset logic: %s %s %s", target_ra, target_dec,
                         self.below_horizon_dec_offset)

        self.custom_goto_state = "start"
        result = self._slew_to_ra_dec([target_ra, target_dec])
        if result == True:
            self.set_target_name(target_name)
            # repeat plate solve and adjust position as needed
            threading.Thread(name=f"goto-dec-offset-thread:{self.device_name}", target=lambda: self.auto_center_thread(target_ra, target_dec)).start()
            return True
        else:
            self.logger.info("Failed to slew")
            return False

    # after we goto_ra_dec, we can do a platesolve and refine until we are close enough
    def auto_center_thread(self, target_ra, target_dec):
        self.logger.info("In auto center logic...")
        self.cur_solve_RA = -9999.0
        self.cur_solve_Dec = -9999.0
        self.custom_goto_state = "working"
        search_count = 0
        while self.schedule['state'] != "stopping" and self.custom_goto_state == "working":
            # wait a bit to ensure we have preview image data
            time.sleep(1)
            self.send_message_param({"method": "start_solve"})
            # reset it immediately so the other wather thread can update the solved position 
            self.cur_solve_RA = -9999.0
            self.cur_solve_Dec = -9999.0
            # if we have not platesolve yet, then repeat
            while self.cur_solve_RA < -1000:
                if self.schedule['state'] == "stopping" or self.custom_goto_state != "working":
                    self.logger.info("auto center thread stopped because the scheduler was requested to stop")
                    self.custom_goto_state = "stopped"
                    return
                time.sleep(1)

            # if we failed platesolve:
            if self.cur_solve_RA == 0 and self.cur_solve_Dec == 0:
                if search_count > 5:
                    self.custom_goto_state = "fail"
                    self.logger.warn(f"auto center failed after {search_count} tries.")
                    return
                else:
                    search_count += 1
                    self.logger.warn(f"Failed to plate solve current position, try # {search_count}. Will try again.")
                    continue

            delta_ra = self.cur_solve_RA - target_ra
            delta_dec = self.cur_solve_Dec - target_dec

            distance_square = delta_ra * delta_ra + delta_dec * delta_dec
            if (distance_square < 1.0e-3):
                self.custom_goto_state = "complete"
                self.logger.info("auto center completed")
                return
            elif search_count <= 7:
                self._sync_target([self.cur_solve_RA, self.cur_solve_Dec])
                self._slew_to_ra_dec([target_ra, target_dec])
                search_count += 1
                self.logger.warn(f"Failed to get close enough to target, try # {search_count}. Will try again.")
            else:
                self.custom_goto_state = "fail"
                self.logger.warn(f"auto center failed after {search_count} tries.")
                return
            
        self.logger.info("auto center thread stopped because the scheduler was requested to stop")
        self.custom_goto_state = "stopped"
        return

    def start_stack(self, params={"gain": Config.init_gain, "restart": True}):
        stack_gain = params["gain"]
        result = self.send_message_param_sync({"method": "iscope_start_stack", "params": {"restart": params["restart"]}})
        self.logger.info(result)
        result = self.send_message_param_sync({"method": "set_control_value", "params": ["gain", stack_gain]})
        self.logger.info(result)
        return not "error" in result

    def get_last_image(self, params):
        album_result = self.send_message_param_sync({"method": "get_albums"})
        album_result = album_result["result"]
        parent_folder = album_result["path"]
        first_list = album_result["list"][0]
        is_subframe = params["is_subframe"]
        result_url = ""
        result_name = None
        self.logger.info("first_list: %s", first_list)
        for files in first_list["files"]:
            if (is_subframe and files["name"].endswith("-sub")) or (
                    not is_subframe and not files["name"].endswith("-sub")):
                result_url = files["thn"]
                result_name = files["name"]
                break
        if not params["is_thumb"]:
            result_url = result_url.partition("_thn.jpg")[0] + ".jpg"
        return {"url": "http://" + self.host + "/" + parent_folder + "/" + result_url,
                "name": result_name}

    # move to a good starting point position specified by lat and lon
    # scheduler state example: {"state":"working", "schedule_id":"abcdefg", 
    #       "result":0, "error":"dummy error",
    #       "cur_schedule_item":{   "type":"mosaic", "schedule_item_GUID":"abcde", "state":"working",
    #                               "stack_status":{"target_name":"test_target", "stack_count": 23, "rejected_count": 2},
    #                               "item_elapsed_time_s":123, "item_remaining_time":-1}
    #       }

    def start_up_thread_fn(self, params):
        try:
            self.schedule['state'] = "working"
            self.logger.info("start up sequence begins ...")
            self.play_sound(80)
            self.schedule['item_number'] = 0     # there is really just one item in this container schedule, with many sub steps
            item_state = {"type": "start_up_sequence", "schedule_item_id": "Not Applicable", "action": "set configurations"}
            self.update_scheduler_state_obj(item_state)
            tz_name = tzlocal.get_localzone_name()
            tz = tzlocal.get_localzone()
            now = datetime.now(tz)
            date_json = {}
            date_json["year"] = now.year
            date_json["mon"] = now.month
            date_json["day"] = now.day
            date_json["hour"] = now.hour
            date_json["min"] = now.minute
            date_json["sec"] = now.second
            date_json["time_zone"] = tz_name
            date_data = {}
            date_data['method'] = 'pi_set_time'
            date_data['params'] = [date_json]

            do_AF = params.get("auto_focus", False)
            do_3PPA = params.get("3ppa", False)
            do_dark_frames = params.get("dark_frames", False)

            loc_data = {}
            loc_param = {}
            # special loc for south pole: (-90, 0)
            if ('lat' not in params or 'lon' not in params) or (params['lat'] <= 0 and params['lon'] <= 0):  # special case of (0,0,) will use the ip address to estimate the location
                if Config.init_lat <= 0 and Config.init_long <= 0:
                    coordinates = Util.get_current_gps_coordinates()
                    if coordinates is not None:
                        latitude, longitude = coordinates
                        self.logger.info(f"Your current GPS coordinates are:")
                        self.logger.info(f"Latitude: {latitude}")
                        self.logger.info(f"Longitude: {longitude}")
                        Config.init_lat = latitude
                        Config.init_long = longitude
            else:
                Config.init_lat = params['lat']
                Config.init_long = params['lon']    

            loc_param['lat'] = Config.init_lat   
            loc_param['lon'] = Config.init_long
            loc_param['force'] = True
            loc_data['method'] = 'set_user_location'
            loc_data['params'] = loc_param
            lang_data = {}
            lang_data['method'] = 'set_setting'
            lang_data['params'] = {'lang': 'en'}

            self.logger.info("verify datetime string: %s", date_data)
            self.logger.info("verify location string: %s", loc_data)

            self.send_message_param_sync({"method": "pi_is_verified"})            
            msg = f"Setting location to {Config.init_lat}, {Config.init_long}"
            self.logger.info(msg)
            self.event_state["scheduler"]["cur_scheduler_item"]["action"]=msg

            self.logger.info(self.send_message_param_sync(date_data))
            response = self.send_message_param_sync(loc_data)
            if "error" in response:
                self.logger.error(f"Failed to set location: {response}")
            else:
                self.logger.info(f"response from set location: {response}")
            self.send_message_param_sync(lang_data)

            self.set_setting(Config.init_expo_stack_ms, Config.init_expo_preview_ms, Config.init_dither_length_pixel, 
                            Config.init_dither_frequency, Config.init_dither_enabled, Config.init_activate_LP_filter)

            self.send_message_param_sync({"method": "pi_output_set2", "params":{"heater":{"state":Config.init_dew_heater_power> 0,"value":Config.init_dew_heater_power}}})

            # save frames setting
            self.send_message_param_sync({"method":"set_stack_setting", "params":{"save_discrete_ok_frame":Config.init_save_good_frames, "save_discrete_frame":Config.init_save_all_frames}})

            msg = "Need to park scope first for a good reference start point"
            self.logger.info(msg)
            self.event_state["scheduler"]["cur_scheduler_item"]["action"]=msg
            response = self.send_message_param_sync({"method":"scope_park"})
            self.logger.info(f"scope park response: {response}")
            if "error" in response:
                msg = "Failed to park scope. Need to restart Seestar and try again."
                self.logger.error(msg)
                return msg
            
            result = self.wait_end_op("ScopeHome")

            if result == True:
                self.logger.info(f"scope_park completed.")
            else:
                self.logger.info(f"scope_park failed.")

            # move the arm up using a thread runner
            # move 10 degrees from polaris
            # first check if a device specific setting is available

            for device in Config.seestars:
                if device['device_num'] == self.device_num:
                    break
            
            lat = Config.scope_aim_lat
            lon = Config.scope_aim_lon

            lat = device.get('scope_aim_lat', lat)   
            lon = device.get('scope_aim_lon', lon)
            self.below_horizon_dec_offset = 0

            if lon < 0:
                lon = 360+lon

            if lat > 80:
                self.logger.warn(f"lat has max value of 80. You requested {lat}.")
                lat = 80

            cur_latlon = self.send_message_param_sync({"method":"scope_get_horiz_coord"})["result"]

            msg = f"moving scope's aim toward a clear patch of sky for HC, from lat-lon {cur_latlon[0]}, {cur_latlon[1]} to {lat}, {lon}"
            self.logger.info(msg)
            self.event_state["scheduler"]["cur_scheduler_item"]["action"]=msg

            while True:
                delta_lat = lat-cur_latlon[0]
                if abs(delta_lat) < 5:
                    break
                elif delta_lat > 0:
                    direction = 90
                else:
                    direction = -90
                if self.move_scope(direction, 1000, 10) == False:
                    break
                time.sleep(0.1)
                cur_latlon = self.send_message_param_sync({"method":"scope_get_horiz_coord"})["result"]
            self.move_scope(0, 0, 0)

            while True:
                delta_lon = lon-cur_latlon[1]
                if abs(delta_lon) < 5:
                    break
                elif delta_lon > 0 or delta_lon < -180:
                    direction = 0
                else:
                    direction = 180
                if self.move_scope(direction, 1000, 10) == False:
                    break
                time.sleep(0.1)
                cur_latlon = self.send_message_param_sync({"method":"scope_get_horiz_coord"})["result"]
            self.move_scope(0, 0, 0)

            cur_latlon = self.send_message_param_sync({"method":"scope_get_horiz_coord"})["result"]
            self.logger.info(f"final lat-lon after move:  {cur_latlon[0]}, {cur_latlon[1]}")

            if self.schedule["state"] != "working":
                return

            result = True
            if do_AF:
                msg = f"auto focus"
                self.logger.info(msg)
                self.event_state["scheduler"]["cur_scheduler_item"]["action"]=msg
                result = self.try_auto_focus(2)
                if result == False:
                    self.logger.warn("Start-up sequence stopped and was unsuccessful.")
                    return
            
            if self.schedule["state"] != "working":
                return
            
            if do_3PPA:
                msg = f"3 point polar alignment"
                self.logger.info(msg)
                self.event_state["scheduler"]["cur_scheduler_item"]["action"]=msg
                result = self.try_3PPA(1)
                if result == False:
                    self.logger.warn("Start-up sequence stopped and was unsuccessful.")
                    return

            if self.schedule["state"] != "working":
                return
            
            if do_dark_frames:
                msg = f"dark frame measurement"
                self.logger.info(msg)
                self.event_state["scheduler"]["cur_scheduler_item"]["action"]=msg
                result = self.try_dark_frame()
                if result == False:
                    self.logger.warn("Start-up sequence stopped and was unsuccessful.")
                    return
            
            if self.schedule["state"] != "working":
                return
                
            if do_3PPA:
                msg = "perform a quick goto routine to confirm and add to the sky model"
                self.logger.info(msg)
                response = self.send_message_param_sync({"method":"get_last_solve_result"})
                last_pos = response["result"]["ra_dec"]
                self.logger.info(f"move from {last_pos[0]}, {last_pos[1]}")
                self.event_state["scheduler"]["cur_scheduler_item"]["action"]=msg
                goto_params = {'is_j2000':False, 'ra': last_pos[0]+0.1, 'dec': last_pos[1]}
                result = self.goto_target(goto_params)
                result = self.wait_end_op("goto_target")
                self.logger.info(f"Goto operation finished with result code: {result}")

            self.logger.info(f"Start-up sequence result: {result}")
            self.event_state["scheduler"]["cur_scheduler_item"]["action"]="complete"
        finally:
            if self.schedule['state'] == "stopping":
                self.sechedule['state'] = "stopped"
            else:
                self.schedule['state'] = "complete"
            self.play_sound(82)

    def action_set_dew_heater(self, params):
        return self.send_message_param_sync({"method": "pi_output_set2", "params":{"heater":{"state":params['heater']> 0,"value":params['heater']}}})

    def action_start_up_sequence(self, params):
        if self.schedule['state'] != "stopped" and self.schedule['state'] != "complete" :
            return self.json_result("start_up_sequence", -1, "Device is busy. Try later.")

        move_up_dec_thread = threading.Thread(name=f"start-up-thread:{self.device_name}", target=lambda: self.start_up_thread_fn(params))
        move_up_dec_thread.start()
        return self.json_result("start_up_sequence", 0, "Sequence started.")

    # {"method":"set_sequence_setting","params":[{"group_name":"Kai_goto_target_name"}]}
    def set_target_name(self, name):
        req = {}
        req['method'] = 'set_sequence_setting'
        params = {}
        params['group_name'] = name
        req['params'] = [params]
        return self.send_message_param_sync(req)

    # scheduler state example: {"state":"working", "schedule_id":"abcdefg", 
    #       "result":0, "error":"dummy error",
    #       "cur_schedule_item":{   "type":"mosaic", "schedule_item_GUID":"abcde",
    #                               "stack_status":{"target_name":"test_target", "action":"blah blah", "stack_count": 23, "rejected_count": 2},
    #                               "item_elapsed_time_s":123, "item_remaining_time_s":-1}
    #       }

    def spectra_thread_fn(self, params):
        try:
            # unlike Mosaic, we can't depend on platesolve to find star, so all movement is by simple motor movement
            center_RA = params["ra"]
            center_Dec = params["dec"]
            is_j2000 = params['is_j2000']
            target_name = params["target_name"]
            session_length = params["session_time_sec"]
            stack_params = {"gain": params["gain"], "restart": True}
            spacing = [5.3, 6.2, 6.5, 7.1, 8.0, 8.9, 9.2, 9.8]
            is_LP = [False, False, True, False, False, False, True, False]
            num_segments = len(spacing)

            item_state = {"type": "spectra", "schedule_item_id": self.schedule['current_item_id'], "target_name":target_name, "action": "slew to target", "item_total_time_s":session_length, "item_remaining_time_s":session_length}
            self.update_scheduler_state_obj(item_state)
            parsed_coord = Util.parse_coordinate(is_j2000, center_RA, center_Dec)
            center_RA = parsed_coord.ra.hour
            center_Dec = parsed_coord.dec.deg

            # 60s for the star
            exposure_time_per_segment = round((session_length - 60.0) / num_segments)
            time_remaining = session_length

            if center_RA < 0:
                center_RA = self.ra
                center_Dec = self.dec
            else:
                # move to target
                self._slew_to_ra_dec([center_RA, center_Dec])

            # take one minute exposure for the star
            if self.schedule['state'] != "working":
                self.schedule['state'] = "stopped"
                return
            self.set_target_name(target_name + "_star")
            if not self.start_stack(stack_params):
                return
            self.event_state["scheduler"]["cur_scheduler_item"]["action"] = "stack for reference star for 60 seconds"
            time.sleep(60)
            self.stop_stack()
            time_remaining -= 60
            self.event_state["scheduler"]["cur_scheduler_item"]["item_remaining_time_s"] = time_remaining

            # capture spectra
            cur_dec = center_Dec
            for index in range(len(spacing)):
                if self.schedule['state'] != "working":
                    self.schedule['state'] = "stopped"
                    return
                cur_dec = center_Dec + spacing[index]
                self.send_message_param_sync({"method": "set_setting", "params": {"stack_lenhance": is_LP[index]}})
                self._slew_to_ra_dec([center_RA, cur_dec])
                self.set_target_name(target_name + "_spec_" + str(index + 1))
                if not self.start_stack(stack_params):
                    return
                self.event_state["scheduler"]["cur_scheduler_item"]["action"] = f"stack for spectra at spacing index {index}"                
                count_down = exposure_time_per_segment
                while count_down > 0:
                    if self.schedule['state'] != "working":
                        self.stop_stack()
                        self.schedule['state'] = "stopped"
                        return
                    time_remaining = session_length - 60 - index*exposure_time_per_segment + count_down
                    time.sleep(10)
                    count_down -= 10
                    time_remaining -= 10
                    self.event_state["scheduler"]["cur_scheduler_item"]["item_remaining_time_s"] = time_remaining
                self.stop_stack()

            self.logger.info("Finished spectra mosaic.")
            self.event_state["scheduler"]["cur_scheduler_item"]["item_remaining_time_s"] = 0
            self.event_state["scheduler"]["cur_scheduler_item"]["action"] = "complete"
        finally:
            self.is_cur_scheduler_item_working = False


    # {"target_name":"kai_Vega", "ra":-1.0, "dec":-1.0, "is_use_lp_filter_too":true, "session_time_sec":600, "grating_lines":300}
    def start_spectra_item(self, params):
        if self.schedule['state'] != "working":
            self.logger.info("Run Scheduler is stopping")
            self.schedule['state'] = "stopped"
            return
        self.is_cur_scheduler_item_working = True
        self.mosaic_thread = threading.Thread(name=f"spectra-thread:{self.device_name}", target=lambda: self.spectra_thread_fn(params))
        self.mosaic_thread.start()
        return "spectra mosiac started"

    def mosaic_goto_inner_worker(self, cur_ra, cur_dec, save_target_name, is_use_autofocus, is_use_LP_filter):
        self.goto_target({'ra': cur_ra, 'dec': cur_dec, 'is_j2000': False, 'target_name': save_target_name})
        result = self.wait_end_op("goto_target")
        self.logger.info(f"Goto operation finished with result code: {result}")

        time.sleep(3)

        if result == True:
            self.send_message_param_sync(
                {"method": "set_setting", "params": {"stack_lenhance": is_use_LP_filter}})
            if is_use_autofocus == True:
                self.event_state["scheduler"]["cur_scheduler_item"]["action"] = "auto focusing"
                result = self.try_auto_focus(2)
            if result == False:
                self.logger.info("Failed to auto focus, but will continue to next panel anyway.")
                result = True
            if result == True:
                # need to check if we have a custom goto running, and make sure it is finished before stacking
                while self.custom_goto_state == "start" or self.custom_goto_state == "working":
                    if self.custom_goto_state == "fail":
                        self.logger.warn("Failed to goto the target with custom goto logic before stacking. Will stop here.")
                        return False
                    time.sleep(3)
                self.custom_goto_state = "stopped"
                time.sleep(4)
                return True
        else:
            self.logger.info("Goto failed.")
            return False


    def mosaic_thread_fn(self, target_name, center_RA, center_Dec, is_use_LP_filter, session_time, nRA, nDec,
                         overlap_percent, gain, is_use_autofocus, selected_panels, num_tries, retry_wait_s):
        try:
            spacing_result = Util.mosaic_next_center_spacing(center_RA, center_Dec, overlap_percent)
            delta_RA = spacing_result[0]
            delta_Dec = spacing_result[1]

            num_panels = nRA*nDec
            is_use_selected_panels = not selected_panels == ""
            if is_use_selected_panels:
                panel_set = selected_panels.split(';')
                num_panels = len(panel_set)
            else:
                panel_set = []

            # adjust mosaic center if num panels is even
            if nRA % 2 == 0:
                center_RA += delta_RA / 2
            if nDec % 2 == 0:
                center_Dec += delta_Dec / 2

            sleep_time_per_panel = round(session_time / nRA / nDec)

            item_remaining_time_s = sleep_time_per_panel * num_panels
            item_state = {"type": "mosaic", "schedule_item_id": self.schedule['current_item_id'], "target_name":target_name, "action": "start", "item_total_time_s":item_remaining_time_s, "item_remaining_time_s":item_remaining_time_s}
            self.update_scheduler_state_obj(item_state)

            cur_dec = center_Dec - int(nDec / 2) * delta_Dec
            for index_dec in range(nDec):
                spacing_result = Util.mosaic_next_center_spacing(center_RA, cur_dec, overlap_percent)
                delta_RA = spacing_result[0]
                cur_ra = center_RA - int(nRA / 2) * spacing_result[0]
                for index_ra in range(nRA):                   
                    if self.schedule['state'] != "working":
                        self.logger.info("Mosaic mode was requested to stop. Stopping")
                        self.schedule['state'] = "stopped"
                        return

                    # check if we are doing a subset of the panels
                    panel_string = str(index_ra + 1) + str(index_dec + 1)
                    if is_use_selected_panels and panel_string not in panel_set:
                        cur_ra += delta_RA
                        continue
                    
                    self.event_state["scheduler"]["cur_scheduler_item"]["cur_ra_panel_num"] = index_ra+1
                    self.event_state["scheduler"]["cur_scheduler_item"]["cur_dec_panel_num"] = index_dec+1 

                    if nRA == 1 and nDec == 1:
                        save_target_name = target_name
                    else:
                        save_target_name = target_name + "_" + panel_string
                    
                    self.logger.info("Stacking operation started for " + save_target_name)
                    self.logger.info("mosaic goto for panel %s, to location %s", panel_string, (cur_ra, cur_dec))

                    # set_settings(x_stack_l, x_continuous, d_pix, d_interval, d_enable, l_enhance, heater_enable):
                    # TODO: Need to set correct parameters
                    self.send_message_param_sync({"method": "set_setting", "params": {"stack_lenhance": False}})

                    for try_index in range(num_tries):
                        try_count = try_index+1
                        self.event_state["scheduler"]["cur_scheduler_item"]["action"] = f"attempt #{try_count} slewing to target panel centered at {cur_ra:.2f}, {cur_dec:.2f}"                 
                        self.logger.info(f"Trying to readch target, attempt #{try_count}")
                        result = self.mosaic_goto_inner_worker(cur_ra, cur_dec, save_target_name, is_use_autofocus, is_use_LP_filter)
                        if result == True:
                            break
                        else:
                            time.sleep(retry_wait_s)

                    self.event_state["scheduler"]["cur_scheduler_item"]["action"] = f"stacking the panel for {sleep_time_per_panel} seconds"
                    if not self.start_stack({"gain": gain, "restart": True}):
                        self.event_state["scheduler"]["cur_scheduler_item"]["action"] = "Failed to start stacking."
                        return True

                    panel_remaining_time_s = sleep_time_per_panel
                    for i in range(round(sleep_time_per_panel/5)):
                        threading.current_thread().last_run = datetime.now()

                        if self.schedule['state'] != "working":
                            self.logger.info("Scheduler was requested to stop. Stopping current mosaic.")
                            self.event_state["scheduler"]["cur_scheduler_item"]["action"] = "Scheduler was requested to stop. Stopping current mosaic."
                            self.stop_stack()
                            self.schedule['state'] = "stopped"
                            return True
                        time.sleep(5)
                        panel_remaining_time_s -= 5
                        item_remaining_time_s -= 5
                        self.event_state["scheduler"]["cur_scheduler_item"]["panel_remaining_time_s"] = panel_remaining_time_s
                        self.event_state["scheduler"]["cur_scheduler_item"]["item_remaining_time_s"] = item_remaining_time_s
                    self.stop_stack()
                    self.logger.info("Stacking operation finished " + save_target_name)
                    cur_ra += delta_RA
                cur_dec += delta_Dec
            self.logger.info("Finished mosaic.")
            self.event_state["scheduler"]["cur_scheduler_item"]["item_remaining_time_s"] = 0
            self.event_state["scheduler"]["cur_scheduler_item"]["action"] = "complete"
        finally:
            self.is_cur_scheduler_item_working = False

    def start_mosaic_item(self, params):
        if self.schedule['state'] != "working":
            self.logger.info("Run Scheduler is stopping")
            self.schedule['state'] = "stopped"
            return
        target_name = params['target_name']
        center_RA = params['ra']
        center_Dec = params['dec']
        is_j2000 = params['is_j2000']
        is_use_LP_filter = params['is_use_lp_filter']
        session_time = params['session_time_sec']
        nRA = params['ra_num']
        nDec = params['dec_num']
        overlap_percent = params['panel_overlap_percent']
        gain = params['gain']
        if 'is_use_autofocus' in params:
            is_use_autofocus = params['is_use_autofocus']
        else:
            is_use_autofocus = False
        if not 'selected_panels' in params:
            selected_panels = ""
        else:
            selected_panels = params['selected_panels']
        num_tries = params.get("num_tries", 1)
        retry_wait_s = params.get("retry_wait_s", 300)

        # verify mosaic pattern
        if nRA < 1 or nDec < 0:
            self.logger.info("Mosaic size is invalid. Moving to next schedule item if any.")
            return

        if not isinstance(center_RA, str) and center_RA == -1 and center_Dec == -1:
            center_RA = self.ra
            center_Dec = self.dec
            is_j2000 = False

        parsed_coord = Util.parse_coordinate(is_j2000, center_RA, center_Dec)
        center_RA = parsed_coord.ra.hour
        center_Dec = parsed_coord.dec.deg

        # print input requests
        self.logger.info("received parameters:")
        self.logger.info("  target        : " + target_name)
        self.logger.info("  RA            : %s", center_RA)
        self.logger.info("  Dec           : %s", center_Dec)
        self.logger.info("  from RA       : %s", self.ra)
        self.logger.info("  from Dec      : %s", self.dec)
        self.logger.info("  use LP filter : %s", is_use_LP_filter)
        self.logger.info("  session time  : %s", session_time)
        self.logger.info("  RA num panels : %s", nRA)
        self.logger.info("  Dec num panels: %s", nDec)
        self.logger.info("  overlap %%    : %s", overlap_percent)
        self.logger.info("  gain          : %s", gain)
        self.logger.info("  use autofocus : %s", is_use_autofocus)
        self.logger.info("  select panels : %s", selected_panels)
        self.logger.info("  # goto tries  : %s", num_tries)
        self.logger.info("  retry wait sec: %s", retry_wait_s)

        self.is_cur_scheduler_item_working = True
        self.mosaic_thread = threading.Thread(
            target=lambda: self.mosaic_thread_fn(target_name, center_RA, center_Dec, is_use_LP_filter, session_time,
                                                 nRA, nDec, overlap_percent, gain, is_use_autofocus, selected_panels, num_tries, retry_wait_s))
        self.mosaic_thread.name = f"MosaicThread:{self.device_name}"
        self.mosaic_thread.start()

    def get_schedule(self, params):
        if 'schedule_id' in params:
            if self.schedule['schedule_id'] != params['schedule_id']:
                return {}
            
        return self.schedule

    def create_schedule(self, params):
        if self.schedule['state'] == "working":
            return "scheduler is still active"
        if self.schedule['state'] == "stopping":
            self.schedule['state'] = "stopped"
        
        if 'schedule_id' in params:
            schedule_id = params['schedule_id']
        else:
            schedule_id = str(uuid.uuid4())

        self.schedule['schedule_id'] = schedule_id
        self.schedule['state'] = self.schedule['state']
        self.schedule['list'].clear()
        return self.schedule

    def construct_schedule_item(self, params):
        if params['action'] == 'start_mosaic':
            mosaic_params = params['params']
            if isinstance(mosaic_params['ra'], str):
                # try to trim the seconds to 1 decimal
                mosaic_params['ra'] = Util.trim_seconds(mosaic_params['ra'])
                mosaic_params['dec'] = Util.trim_seconds(mosaic_params['dec'])
            elif isinstance(mosaic_params['ra'], float):
                if mosaic_params['ra'] < 0:
                    mosaic_params['ra'] = self.ra
                    mosaic_params['dec'] = self.dec
                    mosaic_params['is_j2000'] = False
                mosaic_params['ra'] = round(mosaic_params['ra'], 4)
                mosaic_params['dec'] = round(mosaic_params['dec'], 4)
        params['schedule_item_id'] = str(uuid.uuid4())
        return params

    def add_schedule_item(self, params):
        new_item = self.construct_schedule_item(params)
        self.schedule['list'].append(new_item)
        return self.schedule

    def replace_schedule_item(self, params):
        targeted_item_id = params['item_id']
        index = 0
        if self.schedule['state'] == 'working':
            active_schedule_item_id = self.schedule['current_item_id']
            reached_cur_item = False
            while index < len(self.schedule['list']) and not reached_cur_item:
                item_id = self.schedule['list'][index].get('schedule_item_id', 'UNKNOWN')
                if item_id == targeted_item_id:
                    self.logger.warn("Cannot insert schedule item that has already been executed")
                    return self.schedule
                if item_id == active_schedule_item_id:
                    reached_cur_item = True

        while index < len(self.schedule['list']):
            item = self.schedule['list'][index]
            item_id = item.get('schedule_item_id', 'UNKNOWN')
            if item_id == targeted_item_id:
                new_item = self.construct_schedule_item(params)
                self.schedule['list'][index] = new_item
                break
            index += 1
        return self.schedule

    def insert_schedule_item_before(self, params):
        targeted_item_id = params['before_id']
        index = 0
        if self.schedule['state'] == 'working':
            active_schedule_item_id = self.schedule['current_item_id']
            reached_cur_item = False
            while index < len(self.schedule['list']) and not reached_cur_item:
                item_id = self.schedule['list'][index].get('schedule_item_id', 'UNKNOWN')
                if item_id == targeted_item_id:
                    self.logger.warn("Cannot insert schedule item that has already been executed")
                    return self.schedule
                if item_id == active_schedule_item_id:
                    reached_cur_item = True
                index += 1
        while index < len(self.schedule['list']):
            item = self.schedule['list'][index]
            item_id = item.get('schedule_item_id', 'UNKNOWN')
            if item_id == targeted_item_id:
                new_item = self.construct_schedule_item(params)
                self.schedule['list'].insert(index, new_item)
                break
            index += 1
        return self.schedule

    def remove_schedule_item(self, params):
        targeted_item_id = params['schedule_item_id']
        index = 0
        if self.schedule['state'] == 'working':
            active_schedule_item_id = self.schedule['current_item_id']
            reached_cur_item = False
            while index < len(self.schedule['list']) and not reached_cur_item:
                item_id = self.schedule['list'][index].get('schedule_item_id', 'UNKNOWN')
                if item_id == targeted_item_id:
                    self.logger.warn("Cannot remove schedule item that has already been executed")
                    return self.schedule
                if item_id == active_schedule_item_id:
                    reached_cur_item = True
                index += 1
        while index < len(self.schedule['list']):
            item = self.schedule['list'][index]
            item_id = item.get('schedule_item_id', 'UNKNOWN')
            if item_id == targeted_item_id:
                self.schedule['list'].remove(item)
                break
            index += 1
        return self.schedule

    # shortcut to start a new scheduler with only a mosaic request
    def start_mosaic(self, params):
        if self.schedule['state'] != "stopped" and self.schedule['state'] != "complete":
            return self.json_result("start_mosaic", -1, "An existing scheduler is active. Returned with no action.")
        self.create_schedule(params)
        schedule_item = {}
        schedule_item['action'] = "start_mosaic"
        schedule_item['params'] = params
        self.add_schedule_item(schedule_item)
        return self.start_scheduler(params)

    # shortcut to start a new scheduler with only a spectra request
    def start_spectra(self, params):
        if self.schedule['state'] != "stopped" and self.schedule['state'] != "complete":
            return self.json_result("start_spectra", -1, "An existing scheduler is active. Returned with no action.")
        self.create_schedule(params)
        schedule_item = {}
        schedule_item['action'] = "start_spectra"
        schedule_item['params'] = params
        self.add_schedule_item(schedule_item)
        return self.start_scheduler(params)

    def json_result(self, command_name, code, result):
        if code != 0:
            self.logger.warn(f"Returning not normal result for command {command_name}, code: {code}, result: {result}.")
        else:
            self.logger.debug(f"Returning result for command {command_name}, code: {code}, result: {result}.")

        return {"jsonrpc": "2.0", "TimeStamp":time.time(), "command":command_name, "code":code, "result":result}
    
    def start_scheduler(self, params):
        if "schedule_id" in params and params['schedule_id'] != self.schedule['schedule_id']:
            return self.json_result("start_scheduler", 0, f"Schedule with id {params['schedule_id']} did not match this device's schedule. Returned with no action.")
        if self.schedule['state'] != "stopped" and self.schedule['state'] != "complete":
            return self.json_result("start_scheduler", -1, "An existing scheduler is active. Returned with no action.")
        self.scheduler_thread = threading.Thread(target=lambda: self.scheduler_thread_fn(), daemon=True)
        self.scheduler_thread.name = f"SchedulerThread:{self.device_name}"
        self.scheduler_thread.start()
        self.schedule['state'] = "working"
        return self.schedule

    # scheduler state example: {"state":"working", "schedule_id":"abcdefg", 
    #       "result":0, "error":"dummy error",
    #       "cur_schedule_item":{   "type":"mosaic", "schedule_item_GUID":"abcde", "state":"working",
    #                               "stack_status":{"target_name":"test_target", "stack_count": 23, "rejected_count": 2},
    #                               "item_elapsed_time_s":123, "item_remaining_time":-1}
    #       }

    def scheduler_thread_fn(self):
        def update_time():
            threading.current_thread().last_run = datetime.now()

        self.schedule['state'] = "working"
        issue_shutdown = False
        self.play_sound(80)
        self.logger.info("schedule started ...")
        index = 0
        while index < len(self.schedule['list']):
            update_time()
            if self.schedule['state'] != "working":
                break
            cur_schedule_item = self.schedule['list'][index]
            self.schedule['current_item_id'] = cur_schedule_item.get('schedule_item_id', 'UNKNOWN')
            self.schedule['item_number'] = index+1
            action = cur_schedule_item['action']
            if action == 'start_mosaic':
                self.start_mosaic_item(cur_schedule_item['params'])
                while self.is_cur_scheduler_item_working == True:
                    update_time()
                    time.sleep(2)
            elif action == 'start_spectra':
                self.start_spectra_item(cur_schedule_item['params'])
                while self.is_cur_scheduler_item_working == True:
                    update_time()
                    time.sleep(2)
            elif action == 'auto_focus':
                item_state = {"type": "auto_focus", "schedule_item_id": self.schedule['current_item_id'], "action": "auto focus"}
                self.update_scheduler_state_obj(item_state)
                self.try_auto_focus(cur_schedule_item['params']['try_count'])
            elif action == 'shutdown':
                item_state = {"type": "shut_down", "schedule_item_id": self.schedule['current_item_id'], "action": "shut down"}
                self.update_scheduler_state_obj(item_state)
                self.schedule['state'] = "stopped"
                issue_shutdown = True
                break
            elif action == 'wait_for':
                sleep_time = cur_schedule_item['params']['timer_sec']
                item_state = {"type": "wait_for", "schedule_item_id": self.schedule['current_item_id'], "action": f"wait for {sleep_time} seconds", "remaining s": sleep_time}
                self.update_scheduler_state_obj(item_state)
                sleep_count = 0
                while sleep_count < sleep_time and self.schedule['state'] == "working":
                    update_time()
                    time.sleep(5)
                    sleep_count += 5
                    self.event_state["scheduler"]["cur_scheduler_item"]["remaining s"] = sleep_time - sleep_count

            elif action == 'wait_until':
                wait_until_time = cur_schedule_item['params']['local_time'].split(":")
                wait_until_hour = int(wait_until_time[0])
                wait_until_minute = int(wait_until_time[1])
                local_time = local_time = datetime.now()
                item_state = {"type": "wait_until", "schedule_item_id": self.schedule['current_item_id'], 
                              "action": f"wait until local time of {cur_schedule_item['params']['local_time']}"}
                self.update_scheduler_state_obj(item_state)
                while self.schedule['state'] == "working":
                    update_time()
                    local_time = datetime.now()
                    if local_time.hour == wait_until_hour and local_time.minute == wait_until_minute:
                        break
                    time.sleep(5)
                    self.event_state["scheduler"]["cur_scheduler_item"]["current time"] = f"{local_time.hour:02d}:{local_time.minute:02d}"
            else:
                request = {'method': action, 'params': cur_schedule_item['params']}
                self.send_message_param_sync(request)
            index += 1
        self.reset_below_horizon_dec_offset()

        if self.schedule['state'] != "stopped":
            self.schedule['state'] = "complete"
        self.schedule['current_item_id'] = ""
        self.schedule['item_number'] = 0
        self.logger.info("Scheduler finished.")
        self.play_sound(82)
        if issue_shutdown:
            self.send_message_param_sync({"method":"pi_shutdown"})

    def stop_scheduler(self, params):
        if 'schedule_id' in params and self.schedule['schedule_id'] != params['schedule_id']:
            return self.json_result("stop_scheduler", 0, f"Schedule with id {params['schedule_id']} did not match this device's schedule. Returned with no action.")
            
        if self.schedule['state'] == "working":
            self.schedule['state'] = "stopping"
            self.stop_slew()
            self.stop_stack()
            self.play_sound(83)
            return self.json_result("stop_scheduler", 0, f"Scheduler stopped successfully.")

        elif self.schedule['state'] == "stopped":
            return self.json_result("stop_scheduler", -3, "Scheduler is not running while trying to stop!")
        else:
            return self.json_result("stop_scheduler", -4, "scheduler has already been requested to stop")
        
    def wait_end_op(self, in_op_name):
        if in_op_name == "goto_target":
            self.mark_goto_status_as_start()
            while self.is_goto() == True:
                time.sleep(1)
            return self.is_goto_completed_ok()
            
        else:
            self.event_state[in_op_name] = {"state":"stopped"}
            while in_op_name not in self.event_state or (self.event_state[in_op_name]["state"] != "complete" and self.event_state[in_op_name]["state"] != "fail"):
                time.sleep(1)
            return self.event_state[in_op_name]["state"] == "complete"

    # def sleep_with_heartbeat(self, in_sleep_time):
    #     stacking_timer = 0
    #     while stacking_timer < in_sleep_time:         # stacking time per segment
    #         stacking_timer += 1
    #         time.sleep(1)
    #         print(self.device_name, ": session elapsed ", str(stacking_timer) + "s of " + str(in_sleep_time) + "s", end= "\r")

    # def parse_ra_to_float(self, ra_string):
    #     # Split the RA string into hours, minutes, and seconds
    #     hours, minutes, seconds = map(float, ra_string.split(':'))

    #     # Convert to decimal degrees
    #     ra_decimal = hours + minutes / 60 + seconds / 3600

    #     return ra_decimal

    def parse_dec_to_float(self, dec_string):
        # Split the Dec string into degrees, minutes, and seconds
        if dec_string[0] == '-':
            sign = -1
            dec_string = dec_string[1:]
        else:
            sign = 1
        degrees, minutes, seconds = map(float, dec_string.split(':'))

        # Convert to decimal degrees
        dec_decimal = sign * degrees + minutes / 60 + seconds / 3600

        return dec_decimal

    def start_watch_thread(self):
        # only bail if is_watch_events is true
        if self.is_watch_events:
            return
        else:
            self.is_watch_events = True

            for i in range(3, 0, -1):
                if self.reconnect():
                    self.logger.info(f'Connected')
                    break
                else:
                    self.logger.info(f'Connection Failed, is Seestar turned on?')
                    time.sleep(1)
            else:
                self.logger.info(
                    f'Could not establish connection to Seestar. Starting in offline mode')

            try:
                # Start up heartbeat and receive threads

                self.get_msg_thread = threading.Thread(target=self.receive_message_thread_fn, daemon=True)
                self.get_msg_thread.name = f"IncomingMsgThread:{self.device_name}"
                self.get_msg_thread.start()

                self.heartbeat_msg_thread = threading.Thread(target=self.heartbeat_message_thread_fn, daemon=True)
                self.heartbeat_msg_thread.name = f"HeartbeatMsgThread:{self.device_name}"
                self.heartbeat_msg_thread.start()
            except Exception as ex:
                # todo : Disconnect socket and set is_watch_events false
                pass

    def end_watch_thread(self):
        # I think it should be is_watch_events instead of is_connected...
        if self.is_connected == True:
            self.logger.info("End watch thread!")
            self.is_watch_events = False
            self.get_msg_thread.join(timeout=7)
            self.heartbeat_msg_thread.join(timeout=7)
            self.s.close()
            self.is_connected = False

    def get_events(self):
        while True:
            try:
                if len(self.event_queue) == 0:
                    time.sleep(2)
                    continue
                event = self.event_queue.popleft()
                try:
                    del event["Timestamp"] # Safety first...
                except:
                    pass
                # print(f"Fetched event {self.device_name}")
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-5]
                frame = (b'data: <pre>' +
                        ts.encode('utf-8') +
                        b': ' +
                        json.dumps(event).encode('utf-8') +
                        b'</pre>\n\n')
                yield frame
            except GeneratorExit:
                break
            except:
                time.sleep(2)

