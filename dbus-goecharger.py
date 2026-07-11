#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import platform
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import time
import requests
import threading

sys.path.insert(1, os.path.join(os.path.dirname(__file__),
                                '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

# Formatierungsfunktionen für dbus
_kwh = lambda p, v: f"{v:.2f}kWh"
_a   = lambda p, v: f"{v:.1f}A"
_w   = lambda p, v: f"{v:.1f}W"
_v   = lambda p, v: f"{v:.1f}V"
_degC= lambda p, v: f"{v}°C"
_s   = lambda p, v: f"{v}s"

class DbusGoeChargerService:
    def __init__(self, servicename, deviceinstance, paths=None,
                 productname='go-eCharger', connection='HTTP JSON',
                 voltage_mult=1.0, voltage_off=0.0,
                 current_mult=1.0, current_off=0.0,
                 power_mult=1.0, power_off=0.0,
                 energy_mult=1.0, energy_off=0.0):
        self._dbusservice = VeDbusService(servicename)
        self._paths = paths

        # Kalibrierungsparameter speichern
        self._voltage_mult = voltage_mult
        self._voltage_off = voltage_off
        self._current_mult = current_mult
        self._current_off = current_off
        self._power_mult = power_mult
        self._power_off = power_off
        self._energy_mult = energy_mult
        self._energy_off = energy_off

        logging.debug("%s /DeviceInstance = %d", servicename, deviceinstance)

        # Management
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion',
                                   '1.3.1 (Python ' + platform.python_version() + ')')
        self._dbusservice.add_path('/Mgmt/Connection', connection)

        # Pflichtobjekte
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 65535)
        self._dbusservice.add_path('/ProductName', productname)
        self._dbusservice.add_path('/FirmwareVersion', '1.0.0')
        self._dbusservice.add_path('/HardwareVersion', 0)
        self._dbusservice.add_path('/Connected', 1)

        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/Position', 1)

        # Ladeparameter
        self._dbusservice.add_path('/Current', None, gettextcallback=_a)
        self._dbusservice.add_path('/MaxCurrent', None, gettextcallback=_a, writeable=True)
        self._dbusservice.add_path('/SetCurrent', None, gettextcallback=_a, writeable=True)

        # Hauptmesswerte
        self._dbusservice.add_path('/Power', None, gettextcallback=_w)
        self._dbusservice.add_path('/Ac/Power', None, gettextcallback=_w)
        self._dbusservice.add_path('/Voltage', None, gettextcallback=_v)
        self._dbusservice.add_path('/Ac/Voltage', None, gettextcallback=_v)
        self._dbusservice.add_path('/Energy/Forward', None, gettextcallback=_kwh)
        self._dbusservice.add_path('/Ac/Energy/Forward', None, gettextcallback=_kwh)

        # Phasen
        self._dbusservice.add_path('/L1/Voltage', None, gettextcallback=_v)
        self._dbusservice.add_path('/L2/Voltage', None, gettextcallback=_v)
        self._dbusservice.add_path('/L3/Voltage', None, gettextcallback=_v)
        self._dbusservice.add_path('/L1/Current', None, gettextcallback=_a)
        self._dbusservice.add_path('/L2/Current', None, gettextcallback=_a)
        self._dbusservice.add_path('/L3/Current', None, gettextcallback=_a)
        self._dbusservice.add_path('/L1/Power', None, gettextcallback=_w)
        self._dbusservice.add_path('/L2/Power', None, gettextcallback=_w)
        self._dbusservice.add_path('/L3/Power', None, gettextcallback=_w)

        # Status und Ladezeit
        self._dbusservice.add_path('/Status', 0)
        self._dbusservice.add_path('/ChargingTime', 0, gettextcallback=_s)

        # Lademodus (optional)
        self._dbusservice.add_path('/ChargeMode', 0, writeable=True)

        self._lastUpdate = 0
        self._updateIndex = 0
        self._chargingTime = 0

        try:
            data = self._getGoeChargerData()
            self._updateValues(data)
        except Exception as e:
            logging.warning(f"Initialer Datenabruf fehlgeschlagen: {e}")

        self._goe_thread = threading.Thread(target=self._workerThread)
        self._goe_thread.daemon = True
        self._goe_thread.start()

    def _getIP(self):
        configFile = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.ini')
        if os.path.exists(configFile):
            import configparser
            config = configparser.ConfigParser()
            config.read(configFile)
            if 'ONPREMISE' in config:
                return config['ONPREMISE'].get('Host', '192.168.1.222')
        return '192.168.1.222'

    def _getGoeChargerData(self):
        ip = self._getIP()
        url = f"http://{ip}/status"
        try:
            r = requests.get(url, timeout=3)
            r.raise_for_status()
            data = r.json()
            return data
        except Exception as e:
            logging.warning(f"Fehler beim Abruf von {url}: {e}")
            return {}

    def _fetch_and_update(self):
        try:
            data = self._getGoeChargerData()
            if data:
                self._updateValues(data)
                self._updateIndex += 1
                self._dbusservice['/UpdateIndex'] = self._updateIndex
                self._lastUpdate = time.time()
            else:
                logging.info("Keine Daten empfangen, versuche erneut")
        except Exception as e:
            logging.error(f"Fehler im Update-Zyklus: {e}")

    def _updateValues(self, data):
        # Konvertiere alle String-Werte in Zahlen (die API liefert Strings)
        def _int(key, default=0):
            return int(data.get(key, default))
        def _float(key, default=0.0):
            return float(data.get(key, default))

        nrg = data.get('nrg', [])

        # Spannungen (V) – mit Kalibrierung
        u1 = (float(nrg[0]) * self._voltage_mult + self._voltage_off) if len(nrg) > 0 else 0.0
        u2 = (float(nrg[1]) * self._voltage_mult + self._voltage_off) if len(nrg) > 1 else 0.0
        u3 = (float(nrg[2]) * self._voltage_mult + self._voltage_off) if len(nrg) > 2 else 0.0
        self._dbusservice['/L1/Voltage'] = u1
        self._dbusservice['/L2/Voltage'] = u2
        self._dbusservice['/L3/Voltage'] = u3
        self._dbusservice['/Voltage'] = u1
        self._dbusservice['/Ac/Voltage'] = u1

        # Ströme (A) – in 0.1A, mit Kalibrierung
        i1_raw = float(nrg[6]) if len(nrg) > 6 else 0.0
        i2_raw = float(nrg[4]) if len(nrg) > 4 else 0.0
        i3_raw = float(nrg[5]) if len(nrg) > 5 else 0.0
        i1 = round((i1_raw * 0.1) * self._current_mult + self._current_off, 1)
        i2 = round((i2_raw * 0.1) * self._current_mult + self._current_off, 1)
        i3 = round((i3_raw * 0.1) * self._current_mult + self._current_off, 1)
        self._dbusservice['/L1/Current'] = i1
        self._dbusservice['/L2/Current'] = i2
        self._dbusservice['/L3/Current'] = i3
        self._dbusservice['/Current'] = i1

        # Leistungen (W) – in 0.1kW, mit Kalibrierung
        p1_raw = float(nrg[9]) if len(nrg) > 9 else 0.0
        p2_raw = float(nrg[7]) if len(nrg) > 7 else 0.0
        p3_raw = float(nrg[8]) if len(nrg) > 8 else 0.0
        p1 = round((p1_raw * 100) * self._power_mult + self._power_off)
        p2 = round((p2_raw * 100) * self._power_mult + self._power_off)
        p3 = round((p3_raw * 100) * self._power_mult + self._power_off)
        self._dbusservice['/L1/Power'] = p1
        self._dbusservice['/L2/Power'] = p2
        self._dbusservice['/L3/Power'] = p3

        # Gesamtleistung (W) – nrg[11] in 10W, mit Kalibrierung
        total_power = round(float(nrg[11]) * 10 * self._power_mult + self._power_off) if len(nrg) > 11 else 0
        self._dbusservice['/Power'] = total_power
        self._dbusservice['/Ac/Power'] = total_power

        # Energie (kWh) – eto in 0.1 kWh, mit Kalibrierung
        eto = _float('eto') / 10.0
        energy = round(eto * self._energy_mult + self._energy_off, 2)
        self._dbusservice['/Energy/Forward'] = energy
        self._dbusservice['/Ac/Energy/Forward'] = energy

        # Ladestrom (A) und Maximalstrom
        self._dbusservice['/SetCurrent'] = _float('amp')
        self._dbusservice['/MaxCurrent'] = _float('ama')

        # Status: car als int (1=disconnected, 2=connected, 3=charging, 4=charged, 5=error)
        car = _int('car')
        if car == 1:
            status = 0
        elif car == 2:
            status = 1
        elif car == 3:
            status = 2
        elif car == 4:
            status = 3
        else:
            status = 4
        self._dbusservice['/Status'] = status

        # Ladezeit (nur während des Ladevorgangs)
        if car == 3:
            self._chargingTime = _int('dws')
        else:
            self._chargingTime = 0
        self._dbusservice['/ChargingTime'] = self._chargingTime

        logging.debug(f"Update: Power={total_power}W, Current={i1}A, Energy={energy}kWh, Status={status} (car={car})")

    def _handleChangedValue(self, path, value):
        ip = self._getIP()
        if path == '/SetCurrent':
            try:
                r = requests.get(f"http://{ip}/set?amp={int(value)}", timeout=3)
                logging.info(f"SetCurrent auf {value}A gesetzt: {r.text}")
            except Exception as e:
                logging.error(f"SetCurrent fehlgeschlagen: {e}")
        elif path == '/MaxCurrent':
            try:
                r = requests.get(f"http://{ip}/set?ama={int(value)}", timeout=3)
                logging.info(f"MaxCurrent auf {value}A gesetzt: {r.text}")
            except Exception as e:
                logging.error(f"MaxCurrent fehlgeschlagen: {e}")
        elif path == '/ChargeMode':
            logging.info(f"ChargeMode auf {value} gesetzt (noch nicht implementiert)")
        return True

    def _workerThread(self):
        logging.info("Worker-Thread gestartet")
        while True:
            self._fetch_and_update()
            time.sleep(5)

def main():
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop

    script_dir = os.path.dirname(os.path.realpath(__file__))
    log_dir = os.path.join(script_dir, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'dbus-goecharger.log')

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(log_file, maxBytes=1024*1024, backupCount=3)
        ]
    )

    logging.info("Start go-eCharger DBus service")

    DBusGMainLoop(set_as_default=True)

    config_file = os.path.join(script_dir, 'config.ini')
    deviceinstance = 43
    voltage_mult = 1.0
    voltage_off = 0.0
    current_mult = 1.0
    current_off = 0.0
    power_mult = 1.0
    power_off = 0.0
    energy_mult = 1.0
    energy_off = 0.0

    if os.path.exists(config_file):
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(config_file)
        if 'DEFAULT' in cfg:
            deviceinstance = int(cfg['DEFAULT'].get('Deviceinstance', 43))
        if 'ONPREMISE' in cfg:
            # Kalibrierungswerte lesen
            voltage_mult = float(cfg['ONPREMISE'].get('VoltageMultiplier', 1.0))
            voltage_off = float(cfg['ONPREMISE'].get('VoltageOffset', 0.0))
            current_mult = float(cfg['ONPREMISE'].get('CurrentMultiplier', 1.0))
            current_off = float(cfg['ONPREMISE'].get('CurrentOffset', 0.0))
            power_mult = float(cfg['ONPREMISE'].get('PowerMultiplier', 1.0))
            power_off = float(cfg['ONPREMISE'].get('PowerOffset', 0.0))
            energy_mult = float(cfg['ONPREMISE'].get('EnergyMultiplier', 1.0))
            energy_off = float(cfg['ONPREMISE'].get('EnergyOffset', 0.0))
        else:
            logging.warning("Keine [ONPREMISE] Sektion in config.ini, Kalibrierung deaktiviert")
    else:
        logging.warning("Keine config.ini gefunden, Standardwerte für Kalibrierung")

    servicename = f'com.victronenergy.evcharger.http_{deviceinstance:03d}'

    pvac_output = DbusGoeChargerService(
        servicename=servicename,
        deviceinstance=deviceinstance,
        voltage_mult=voltage_mult,
        voltage_off=voltage_off,
        current_mult=current_mult,
        current_off=current_off,
        power_mult=power_mult,
        power_off=power_off,
        energy_mult=energy_mult,
        energy_off=energy_off
    )

    from gi.repository import GLib
    mainloop = GLib.MainLoop()
    mainloop.run()

if __name__ == "__main__":
    main()
