#!/usr/bin/env python3
import sys
import os
from pathlib import Path
import random
import requests
import json
import toml
sys.path.insert(0, str(Path.cwd().parent))
from pyindi.device import (device as Device, INumberVector, ISwitchVector,
                           INumber, ISwitch, IPerm, IPState, ISState, ISRule)
from astropy import units
from astropy.coordinates import SkyCoord

"""
This file uses a skeleton xml file to initialize and
define properties for the Seestar S50. Similar to this example at indilib:
https://www.indilib.org/developers/driver-howto.html#h2-properties
"""

class SeestarDevice(Device):

    def __init__(self, name=None, number=1):
        """
        Construct device with name and number
        """
        super().__init__(name=name)
        self.number = number
        self.url = f'http://localhost:5555/api/v1/telescope/{number}/action'
        self.headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }

    def ISGetProperties(self, device=None):
        """
        Property definitions are generated
        by initProperties and buildSkeleton. No
        need to do it here. """
        ra =  INumber( "RA", "%2.8f", 0, 24, 1, 0, label="RA" )
        dec = INumber( "DEC", "%2.8f", -90, 90, 1, -90, label="DEC" )
        coord = INumberVector([ra, dec], self._devname, "EQUATORIAL_EOD_COORD",
                              IPState.OK, IPerm.RW, label="EQUATORIAL_EOD_COORD")
 
        connect = ISwitch("CONNECT", ISState.OFF, "Connect", )
        disconnect = ISwitch("DISCONNECT", ISState.ON, "Disconnect")
        conn = ISwitchVector([connect, disconnect], self._devname, "CONNECTION",
                IPState.IDLE, ISRule.ONEOFMANY, 
                IPerm.RW, label="Connection")
                
        slew = ISwitch("SLEW", ISState.ON, "Slew", )
        track = ISwitch("TRACK", ISState.OFF, "Track")
        sync = ISwitch("SYNC", ISState.OFF, "Sync")
        oncoordset = ISwitchVector([slew, track, sync], self._devname, "ON_COORD_SET",
                IPState.IDLE, ISRule.ONEOFMANY, 
                IPerm.RW, label="On coord set")
                
        self.IDDef(coord, None)
        self.IDDef(conn, None)
        self.IDDef(oncoordset, None)
        

    def ISNewText(self, device, name, names, values):
        """
        A text vector has been updated from 
        the client. 
        """
        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        self.IUUpdate(device, name, names, values, Set=True)

    def ISNewNumber(self, device, name, names, values):
        """
        A number vector has been updated from the client.
        """
        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")
        
        if name == "EQUATORIAL_EOD_COORD":
            current = self.__getitem__("EQUATORIAL_EOD_COORD")
            ra, dec = float(current['RA'].value), float(current['DEC'].value)
            
            self.IDMessage(f"Current pointing: RA={ra}, Dec={dec}")
            
            for index, value in enumerate(values):
                if value == 'RA':
                    ra = names[index]
                elif value == 'DEC':
                    dec = names[index]
                    
            self.IDMessage(f"Requested RA/Dec: ({ra}, {dec})")

            switch = self.__getitem__('ON_COORD_SET')
            if switch['SLEW'].value == 'On' or switch['TRACK'].value == 'On':
                # Slew/GoTo requested
                if self.goToInProgress():
                    self.terminateGoTo()
                target = SkyCoord(ra * units.hourangle, dec * units.deg)
                ra_hms = target.ra.to_string(unit=units.hourangle, sep=('h', 'm', 's'))
                dec_dms = target.dec.to_string(unit=units.deg, sep=('d', 'm', 's'))
                
                self.IDMessage(f"Requested RA/Dec (str): ({ra_hms}, {dec_dms})")
                
                payload = {
                    "Action": "goto_target",
                    "Parameters": f'{{"target_name":"Stellarium Target", "ra":"{ra_hms}", "dec":"{dec_dms}", "is_j2000":false}}',
                    "ClientID": "1",
                    "ClientTransactionID": "999"
                }
            else:
                # Sync requested
                payload = {
                    "Action": "method_sync",
                    "Parameters": f'{{"method":"scope_sync","params":[{ra}, {dec}]}}',
                    "ClientID": "1",
                    "ClientTransactionID": "999"
                }
            
            try:
                response = requests.put(self.url, data=payload, headers=self.headers)
                
                print(response.json())
                
            except Exception as error:
                self.IDMessage(f"Seestar command error: {error}")
                

    def ISNewSwitch(self, device, name, names, values):
        """
        A switch has been updated from the client.
        """

        self.IDMessage(f"Updating {device} {name} with {dict(zip(names, values))}")

        if name == "CONNECTION":
            try:
                conn = self.IUUpdate(device, name, names, values)
                if conn["CONNECT"].value == 'Off':
                    conn.state = "Idle"
                else:
                    conn.state = "Ok"

                self.IDSet(conn)

            except Exception as error:
                self.IDMessage(f"Error updating CONNECTION property: {error}")
                raise
        else:
            try:
                prop = self.IUUpdate(device, name, names, values)
                self.IDSet(prop)
            except Exception as error:
                self.IDMessage(f"Error updating {name} property: {error}")
                raise
            
    @Device.repeat(2000)
    def do_repeat(self):
        """
        This function is called every 2000.
        """

        conn = self.__getitem__("CONNECTION")
        if conn["CONNECT"].value == 'Off':
            # return
            pass
            
        self.IDMessage("Running repeat function")
        
        payload = {
            "Action": "method_sync",
            "Parameters": "{\"method\":\"scope_get_equ_coord\"}",
            "ClientID": "1",
            "ClientTransactionID": "999"
        }
        
        try:
            response = requests.put(self.url, data=payload, headers=self.headers)

            # parse response and update number vector
            json = response.json()
            result = json['Value']['result']
            ra = result['ra']
            dec = result['dec']
            self.IUUpdate(self._devname, 'EQUATORIAL_EOD_COORD', [ra, dec], ['RA', 'DEC'], Set=True)
            
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")

    def goToInProgress(self):
        """
        Return true if a GoTo is in progress, false otherwise
        """
        payload = {
            "Action": "method_sync",
            "Parameters": "{\"method\":\"get_view_state\"}",
            "ClientID": "1",
            "ClientTransactionID": "999"
        }
        
        try:
            response = requests.put(self.url, data=payload, headers=self.headers)
            json = response.json()
            result = json['Value']['result']
            return result['View']['stage'] == 'AutoGoto'
        
        except Exception as error:
            self.IDMessage(f"Seestar communication error: {error}")
        
    def terminateGoTo(self):
        """
        Terminates current GoTo operation
        """
        payload = {
            "Action": "method_sync",
            "Parameters": "{\"method\":\"iscope_stop_view\",\"params\":{\"stage\":\"AutoGoto\"}}",
            "ClientID": "1",
            "ClientTransactionID": "999"
        }
        
        try:
            response = requests.put(self.url, data=payload, headers=self.headers)
        
        except Exception as error:
            self.IDMessage(f"Error terminating GoTo: {error}")

name = os.environ['INDIDEV']
number = int(os.environ['INDICONFIG'])  #hijack to obtain device number
ss = SeestarDevice(name, number)
ss.start() 
 