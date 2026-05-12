"""Puara Serial Manager.

Usage:
  puara_serial_manager.py <wifiSSID> <wifiPSK> <ipAddress> <port> <fd>
  puara_serial_manager.py -h | --help
  puara_serial_manager.py -v | --version

Options:
  -h --help     Show this screen.
  -v --version  Show version.
"""

# Standard libraries
from collections import namedtuple
from string import Template
from typing import NamedTuple, Optional
import json
import logging
import os
import threading
from time import sleep

# Third-party libraries
from docopt import docopt
import serial
import serial.tools.list_ports

# Local libraries
from wired import tstick, tstick_serial, tstick_osc

BAUDRATE = 115200
READ_TIMEOUT_SECS = 5.0

CONFIG_TEMPLATE = '''
{
    "wifiSSID": "$ssid",
    "wifiPSK": "$psk",
    "oscPORT1": $osc_port,
    "oscIP1": "$ip_addr"
}
'''

DATA_START = b'<<<'
DATA_END = b'>>>'

WifiNetwork = namedtuple("WifiNetwork", ['ssid', 'psk'])

fo = None

class Config(NamedTuple):
    ip_addr: str
    port: int

class Device(NamedTuple):
    device_id: str
    ser: serial.Serial
    name: Optional[str]=None

    def __str__(self):
        if self.name is None:
            return self.device_id
        else:
            return self.name

    def named_device(self, name):
        return Device(self.device_id, self.ser, name)


class PuaraSerialException(Exception):
    pass


class SerialManager:
    def __init__(self, wifi: WifiNetwork, ip_address: Optional[str]=None, port: Optional[str]=None, config_delegate=None):
        self.wifi = wifi
        self.devices = {}
        self.scanner = threading.Thread(target=self.scan_thread, daemon=True)
        self.scanner.start()
        self.config = Config(ip_address, port)
        self.config_delegate = config_delegate
        self.config_template = Template(CONFIG_TEMPLATE)
        self.wired_tsticks = tstick.all_tsticks()

    def scan_thread(self):
        while True:
            for port in serial.tools.list_ports.comports():
                device_id = port.serial_number
                if not device_id:
                    device_id = port.hwid
                if device_id in self.devices:
                    pass
                    # TODO(p42ul): do something with these?
                # Wired T-Stick
                elif device_id in [ts.ser() for ts in self.wired_tsticks]:
                    tstick = [ts for ts in self.wired_tsticks if device_id == ts.ser()][0]
                    config_data = self.config_delegate(tstick.name())
                    lexer = tstick.lexer()
                    parser = tstick.parser()
                    baudrate = tstick.baudrate()
                    sender = tstick_osc.OSCSender(tstick.name(), '127.0.0.1', config_data.port)
                    lexer.subscribe(parser.parse)
                    parser.subscribe(sender.send)
                    self.devices[device_id] = tstick_serial.TStickSerial(port.device, baudrate, tstick_serial.READ_SIZE, lexer.enqueue, b's')
                else:
                    ser = serial.Serial()
                    ser.port, ser.baudrate, ser.timeout = port.device, BAUDRATE, READ_TIMEOUT_SECS
                    device = Device(device_id, ser)
                    self.devices[device_id] = device
                    fo.write(f'found new serial device {device}\n')
                    fo.flush()
                    try:
                        ser.open()
                    except serial.SerialException as e:
                        fo.write(f"Couldn't open serial device {device}, got error {e}\n")
                        fo.flush()
                    threading.Thread(target=self.configure_device, args=[device]).start()
            sleep(1)

    def create_config(self, config):
        return self.config_template.substitute(ip_addr=config.ip_addr, osc_port=config.port,
                    ssid=self.wifi.ssid, psk=self.wifi.psk)

    def configure_device(self, device: Device):
        fo.write(f'waiting for {device} to become ready...\n')
        fo.flush()
        self.wait_for_device_ready(device)
        fo.write(f'{device} ready.\n')
        fo.flush()
        if device.name is None:
            name = self.get_device_name(device)
            device = device.named_device(name)
            fo.write(f'{device.device_id} is {device.name}\n')
            fo.flush()
        fo.write(f'getting config data from {device}\n')
        fo.flush()
        config_data = self.get_config_data(device)
        config_json = json.loads(config_data)
        if self.config_delegate:
            desired_data = self.create_config(self.config_delegate(device.name))
        else:
            desired_data = self.create_config(self.config)
        desired_json = json.loads(desired_data)
        needs_config = False
        for k, desired_v in desired_json.items():
            if not config_json[k] == desired_v:
                needs_config = True
                fo.write(f'value of {k} does not match | desired: "{desired_v}" device: "{config_json[k]}"\n')
                fo.flush()
        if needs_config:
            fo.write(f'configuring {device}\n')
            fo.flush()
            device.ser.write(f'sendconfig {json.dumps(desired_json)}'.encode('utf-8'))
            # TODO(p42ul): Replace this with an ACK from the device side.
            sleep(3)
            device.ser.write(b'writeconfig')
            sleep(3)
            fo.write(f'{device} has been successfully configured. rebooting to initialize config\n')
            fo.flush()
            device.ser.write(b'reboot')
            device.ser.close()

    def wait_for_device_ready(self, device: Device):
        ser = device.ser
        expected = b'pong'
        while True:
            ser.write(b'ping')
            data = ser.read_until(expected=expected)
            if expected in data:
                break
            sleep(READ_TIMEOUT_SECS)
        ser.reset_output_buffer()
        sleep(READ_TIMEOUT_SECS)

    def get_response(self, device: Device, request: str) -> str:
        ser = device.ser
        ser.write(request.encode('utf-8'))
        start_data = ser.read_until(expected=DATA_START)
        if DATA_START not in start_data:
            raise PuaraSerialException(f'received {start_data} in response to "{request}" from {device} on {ser.port}')
        response_data = ser.read_until(expected=DATA_END)
        if DATA_END not in response_data:
            raise PuaraSerialException(f'"{DATA_END}" not received in time from {device} on {ser.port}')
        response_data = response_data.decode('utf-8', 'backslashreplace')
        response_data = response_data.strip(str(DATA_END, 'utf-8'))
        return response_data

    def get_config_data(self, device) -> str:
        return self.get_response(device, 'readconfig')

    def get_device_name(self, device) -> str:
        return self.get_response(device, 'whatareyou')

    def print_available_ports(self):
        for port in serial.tools.list_ports.comports():
            fo.write(f'{port}: hwid {port.hwid} vid {port.vid} pid {port.pid}\n')
            fo.flush()


def main(args):
    global fo
    fo = os.fdopen(int(args['<fd>']), 'w')

    wifi = WifiNetwork(ssid=args['<wifiSSID>'], psk=args['<wifiPSK>'])
    _ = SerialManager(wifi, args['<ipAddress>'], args['<port>'])
    while True:
        sleep(1)


if __name__ == '__main__':
    args = docopt(__doc__, version="Puara Serial Manager 0.1")
    main(args)
