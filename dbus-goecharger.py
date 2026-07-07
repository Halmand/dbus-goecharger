#!/usr/bin/env python3

# import normal packages
import platform
import logging
from logging.handlers import RotatingFileHandler
import sys
import os
import time
import requests  # for http GET
import configparser  # for config/ini file

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusGoeChargerService:
    def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
        config = self._getConfig()
        deviceinstance = int(config['DEFAULT']['Deviceinstance'])
        hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
        acPosition = int(config['DEFAULT']['AcPosition'])
        pauseBetweenRequests = int(config['ONPREMISE']['PauseBetweenRequests'])

        if pauseBetweenRequests <= 20:
            raise ValueError("Pause between requests must be greater than 20")

        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), register=False)
        self._paths = paths

        # Create the mandatory objects
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        self._dbusservice.add_path('/Connected', 1)
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/Position', acPosition)

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(path, settings['initial'], gettextcallback=settings['textformat'])

        # register the service
        self._dbusservice.register()

        # last update
        self._lastUpdate = 0

        # charging time in float
        self._chargingTime = 0.0

        # add _update function 'timer'
        gobject.timeout_add(pauseBetweenRequests, self._update)

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read(os.path.join(os.path.dirname(__file__), 'config.ini'))
        return config

    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT'].get('SignOfLifeLog', '0')
        if not value:
            value = 0
        return int(value)

    def _getGoeChargerStatusUrl(self):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']
        host = config['ONPREMISE']['Host']
        if accessType == 'OnPremise':
            URL = "http://%s/status" % host
        else:
            URL = "http://%s/status" % host
        return URL

    def _getGoeChargerData(self, filter=None):
        try:
            url = self._getGoeChargerStatusUrl()
            if filter:
                url += "?filter=" + filter
            request_data = requests.get(url=url, timeout=10)

            if not request_data:
                return None

            json_data = request_data.json()

            if not json_data:
                return None

            return json_data

        except Exception as e:
            logging.warning("Error fetching go-eCharger data: %s", e)
            return None

    def _setGoeChargerValue(self, parameter, value):
        config = self._getConfig()
        accessType = config['DEFAULT']['AccessType']
        host = config['ONPREMISE']['Host']

        if accessType == 'OnPremise':
            URL = "http://%s/status" % host
        else:
            URL = "http://%s/status" % host

        try:
            request_data = requests.get(url=URL, timeout=10)
            if not request_data:
                return False
            json_data = request_data.json()
            if not json_data:
                return False
            if json_data.get(parameter) == str(value):
                return True
            else:
                return False
        except Exception as e:
            logging.warning("Error setting go-eCharger value: %s", e)
            return False

    def _update(self):
        try:
            # get data from go-eCharger
            data = self._getGoeChargerData()

            if data is not None:
                config = self._getConfig()

                # --- Calibration offsets & multipliers ---
                voltageOffset = float(config['ONPREMISE'].get('VoltageOffset', 0))
                voltageMultiplier = float(config['ONPREMISE'].get('VoltageMultiplier', 1.0))
                currentOffset = float(config['ONPREMISE'].get('CurrentOffset', 0))
                currentMultiplier = float(config['ONPREMISE'].get('CurrentMultiplier', 1.0))
                powerOffset = float(config['ONPREMISE'].get('PowerOffset', 0))
                powerMultiplier = float(config['ONPREMISE'].get('PowerMultiplier', 1.0))

                hardwareVersion = int(config['DEFAULT']['HardwareVersion'])

                '''
                data['nrg'] - Firmware 42.0 API v1 units:
                0  = U L1  (Volts, direct)
                1  = U L2  (Volts, direct)
                2  = U L3  (Volts, direct)
                3  = U N   (Volts, direct)
                4  = I L1  (0.1 A)   -> divide by 10
                5  = I L2  (0.1 A)   -> divide by 10
                6  = I L3  (0.1 A)   -> divide by 10
                7  = P L1  (0.1 kW)  -> multiply by 100 = Watts
                8  = P L2  (0.1 kW)  -> multiply by 100 = Watts
                9  = P L3  (0.1 kW)  -> multiply by 100 = Watts
                10 = P N   (0.1 kW)
                11 = P tot (0.01 kW) -> multiply by 10 = Watts
                12 = PF L1 (%)
                13 = PF L2 (%)
                14 = PF L3 (%)
                15 = PF N  (%)
                '''

                # --- Voltage (direct in Volts) ---
                voltage_raw = int(data['nrg'][0])
                self._dbusservice['/Ac/Voltage'] = int(round(voltage_raw * voltageMultiplier + voltageOffset))

                # --- Current (0.1 A -> A) ---
                current_raw = max(data['nrg'][4], data['nrg'][5], data['nrg'][6]) / 10.0
                current_cal = current_raw * currentMultiplier + currentOffset
                self._dbusservice['/Current'] = round(current_cal, 1)

                # --- Power per phase (0.1 kW -> W) ---
                power_l1_raw = int(data['nrg'][7] * 100)
                power_l2_raw = int(data['nrg'][8] * 100)
                power_l3_raw = int(data['nrg'][9] * 100)
                self._dbusservice['/Ac/L1/Power'] = int(round(power_l1_raw * powerMultiplier + powerOffset))
                self._dbusservice['/Ac/L2/Power'] = int(round(power_l2_raw * powerMultiplier + powerOffset))
                self._dbusservice['/Ac/L3/Power'] = int(round(power_l3_raw * powerMultiplier + powerOffset))

                # --- Total Power (0.01 kW -> W) ---
                power_total_raw = int(data['nrg'][11] * 10)
                self._dbusservice['/Ac/Power'] = int(round(power_total_raw * powerMultiplier + powerOffset))

                # --- Energy ---
                energyTotal = int(data['dws']) / 1000.0 / 60.0 / 60.0  # dws is 0.001 kWh
                self._dbusservice['/Ac/Energy/Forward'] = round(energyTotal, 2)

                # --- Charging time ---
                timeDelta = time.time() - self._lastUpdate
                if powerTotal_raw > 10:
                    self._chargingTime += timeDelta
                else:
                    self._chargingTime = 0

                if hardwareVersion >= 3:
                    self._dbusservice['/ChargingTime'] = int(self._chargingTime)
                else:
                    self._dbusservice['/ChargingTime'] = int(self._chargingTime)

                # --- Status ---
                status = 0
                car = int(data['car'])
                if car == 1:
                    status = 0  # Not ready
                elif car == 2:
                    status = 2  # Charging
                elif car == 3:
                    status = 6  # Car unplugged / ready
                elif car == 4:
                    status = 3  # Error
                self._dbusservice['/Status'] = status

                # --- UpdateIndex ---
                index = self._dbusservice['/UpdateIndex'] + 1
                if index > 255:
                    index = 0
                self._dbusservice['/UpdateIndex'] = index

                # update lastupdate
                self._lastUpdate = time.time()

            else:
                logging.debug("Wallbox is not available")

        except Exception as e:
            logging.warning("Error in _update: %s", e)

        return True

    def _handlechangedvalue(self, path, value):
        if path == '/SetCurrent':
            return self._setGoeChargerValue('amp', value)
        elif path == '/StartStop':
            return self._setGoeChargerValue('alw', value)
        elif path == '/MaxCurrent':
            return self._setGoeChargerValue('ama', value)
        else:
            return False


def main():
    # configure logging
    config = configparser.ConfigParser()
    config.read(os.path.join(os.path.dirname(__file__), 'config.ini'))
    logging_level = config["DEFAULT"].get("Logging", "DEBUG").upper()

    logging.basicConfig(
        level=logging_level,
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                os.path.dirname(__file__) + '/logs/dbus-goecharger.log',
                maxBytes=1000000,
                backupCount=3
            )
        ])

    try:
        logging.info("Start go-eCharger DBus service")

        from dbus.mainloop.glib import DBusGMainLoop
        DBusGMainLoop(set_as_default=True)

        # formatting lambdas
        _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
        _a   = lambda p, v: (str(round(v, 1)) + 'A')
        _w   = lambda p, v: (str(round(v, 1)) + 'W')
        _v   = lambda p, v: (str(round(v, 1)) + 'V')
        _degC = lambda p, v: (str(v) + '°C')
        _s   = lambda p, v: (str(v) + 's')

        deviceinstance = int(config["DEFAULT"]["Deviceinstance"])
        hardwareVersion = int(config["DEFAULT"].get("HardwareVersion", "2"))

        # start our main-service
        pvac_output = DbusGoeChargerService(
            servicename='com.victronenergy.evcharger',
            paths={
                '/Current':          {'initial': 0, 'textformat': _a},
                '/Ac/Power':         {'initial': 0, 'textformat': _w},
                '/Ac/L1/Power':      {'initial': 0, 'textformat': _w},
                '/Ac/L2/Power':      {'initial': 0, 'textformat': _w},
                '/Ac/L3/Power':      {'initial': 0, 'textformat': _w},
                '/Ac/Voltage':       {'initial': 0, 'textformat': _v},
                '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
                '/UpdateIndex':      {'initial': 0, 'textformat': _s},
            }
        )

        mainloop = gobject.MainLoop()
        mainloop.run()

    except Exception as e:
        logging.critical('Error at %s', 'main', exc_info=e)


if __name__ == "__main__":
    main()
