from __future__ import print_function
from enum import IntEnum
import struct
import binascii
import uuid
import threading

from .notifications import GenericClearTileNotification
from .parser import MsftBandParser
from .commands import (
    SERIAL_NUMBER_REQUEST, CARGO_NOTIFICATION,
    GET_TILES_NO_IMAGES, CORE_WHO_AM_I, CORE_GET_API_VERSION,
    SET_THEME_COLOR, START_STRIP_SYNC_END, CORE_SDK_CHECK,
    START_STRIP_SYNC_START, READ_ME_TILE_IMAGE, CORE_GET_VERSION,
    WRITE_ME_TILE_IMAGE_WITH_ID,
    CARGO_SYSTEM_SETTINGS_OOBE_COMPLETED_GET,
    NAVIGATE_TO_SCREEN, GET_ME_TILE_IMAGE_ID,
    GET_TILES, SET_TILES,
)
from .versions import BandType, DeviceVersion, FirmwareVersion
from .socket import BandSocket
from .sensors import decode_sensor_reading
from . import PUSH_SERVICE_PORT


class DummyWrapper:
    def print(self, *args, **kwargs):
        print(*args, **kwargs)

    def send(self, signal, args):
        print(signal, args)

    def atexit(self, func):
        import atexit
        atexit.register(func)


class FirmwareApp(IntEnum):
    OneBL = 1
    TwoUp = 2
    App = 3
    UpApp = 4


class FirmwareSdkCheckPlatform(IntEnum):
    WindowsPhone = 1
    Windows = 2
    Desktop = 3


class PushServicePacketType(IntEnum):
    WakeApp = 0
    RemoteSubscription = 1
    Sms = 100
    DismissCall = 101
    VoicePacketBegin = 200
    VoicePacketData = 201
    VoicePacketEnd = 202
    VoicePacketCancel = 203
    StrappEvent = 204
    StrappSyncRequest = 205
    CortanaContext = 206
    Keyboard = 220
    KeyboardSetContent = 222


class BandDevice:
    address = ""
    cargo = None
    push = None
    tiles = None
    band_language = None
    band_name = None
    serial_number = None
    push_thread = None
    services = {}
    version: DeviceVersion
    wrapper = DummyWrapper()

    def __init__(self, address):
        self.address = address
        self.push = BandSocket(self, PUSH_SERVICE_PORT)
        self.cargo = BandSocket(self)
        self.wrapper.atexit(self.disconnect)

    @property
    def band_type(self):
        if self.version:
            return self.version.band_type
        return BandType.Unknown

    def connect(self):
        self.cargo.connect()

        # fetch device data
        self.get_firmware_version()

        # start push thread
        self.push_thread = threading.Thread(target=self.listen_pushservice)
        self.push_thread.start()

    def disconnect(self):
        self.push.disconnect()
        self.cargo.disconnect()

    def check_if_oobe_completed(self):
        result, data = self.cargo.cargo_read(
            CARGO_SYSTEM_SETTINGS_OOBE_COMPLETED_GET, 4)
        if data:
            return struct.unpack("<I", data[0])[0] != 0
        return False

    def get_me_tile_image_id(self):
        result, data = self.cargo.cargo_read(GET_ME_TILE_IMAGE_ID, 4)
        if data:
            return data[0]
        return 0

    def get_me_tile_image(self):
        """
        Sends READ_ME_TILE_IMAGE command to device and returns a bgr565
        byte array with Me tile image
        """
        # calculate byte count based on device type
        if self.band_type == BandType.Cargo:
            byte_count = 310 * 102 * 2
        elif self.band_type == BandType.Envoy:
            byte_count = 310 * 128 * 2
        else:
            byte_count = 0

        # read Me Tile image
        result, data = self.cargo.cargo_read(READ_ME_TILE_IMAGE, byte_count)
        pixel_data = b''.join(data)
        return pixel_data

    def set_me_tile_image(self, pixel_data, image_id):
        result, data = self.cargo.cargo_write_with_data(
            WRITE_ME_TILE_IMAGE_WITH_ID,
            pixel_data,
            struct.pack("<I", image_id))
        return result, data

    def navigate_to_screen(self, screen):
        """
        Tells the device to navigate to a given screen.
        AFAIK works only with OOBE screens in OOBE mode
        """
        return self.cargo.cargo_write_with_data(
            NAVIGATE_TO_SCREEN, struct.pack("<H", screen))

    def process_push(self, guid, command, message):
        for service in self.services.values():
            if service.guid == guid:
                new_message = service.push(guid, command, message)
                if new_message:
                    message = new_message
                    break
        return message

    def process_tile_callback(self, result):
        opcode = struct.unpack("I", result[6:10])[0]
        guid = uuid.UUID(bytes_le=result[10:26])
        command = result[26:44]
        tile_name = MsftBandParser.bytes_to_text(result[44:84])

        message = {
            "opcode": opcode,
            "guid": str(guid),
            "command": binascii.hexlify(command),
            "tile_name": tile_name,
        }
        message = self.process_push(guid, command, message)
        self.wrapper.send("PushService", message)

    def process_notification_callback(self, result):
        opcode = struct.unpack("I", result[2:6])[0]
        guid = uuid.UUID(bytes_le=result[6:22])
        command = result[22:]

        message = {
            "opcode": opcode,
            "guid": str(guid),
            "command": str(binascii.hexlify(command)),
        }

        message = self.process_push(guid, command, message)
        self.wrapper.send("PushService", message)

    def listen_pushservice(self):
        self.push.connect()
        while True:
            try:
                result = self.push.receive()
            except OSError:
                break

            packet_type = struct.unpack("H", result[0:2])[0]
            self.wrapper.print(PushServicePacketType(packet_type))

            if packet_type == PushServicePacketType.RemoteSubscription:
                sensor = decode_sensor_reading(result)
                self.wrapper.print(sensor)
            elif packet_type == PushServicePacketType.Sms:
                self.process_notification_callback(result)
            elif packet_type == PushServicePacketType.DismissCall:
                self.process_notification_callback(result)
            elif packet_type == PushServicePacketType.StrappEvent:
                self.wrapper.print(binascii.hexlify(result))
                self.process_tile_callback(result)
            else:
                self.wrapper.print(binascii.hexlify(result))

    def sync(self):
        for service in self.services.values():
            self.wrapper.print(f'{service}'.ljust(80), end='')
            try:
                result = getattr(service, "sync")()
            except Exception as exc:
                self.wrapper.print(exc)
                result = False
            self.wrapper.print("[%s]" % ("OK" if result else "FAIL"))
        self.wrapper.print("Sync finished")

    def clear_tile(self, guid):
        self.send_notification(GenericClearTileNotification(guid))

    def set_theme(self, colors):
        """
        Takes an array of 6 colors encoded as ints

        Base, Highlight, Lowlight, SecondaryText, HighContrast, Muted
        """
        self.cargo.cargo_write(START_STRIP_SYNC_START)
        colors = struct.pack("I"*6, *[int(x) for x in colors])
        self.cargo.cargo_write_with_data(SET_THEME_COLOR, colors)
        self.cargo.cargo_write(START_STRIP_SYNC_END)

    def get_tiles(self):
        if not self.tiles:
            self.request_tiles()
        return self.tiles

    def get_serial_number(self):
        if not self.serial_number:
            # ask nicely for serial number
            result, number = self.cargo.cargo_read(SERIAL_NUMBER_REQUEST, 12)
            if result:
                self.serial_number = number[0].decode("utf-8")
        return self.serial_number

    def get_max_tile_capacity(self):
        # TODO: actual logic for calculating that
        return 15

    def set_tiles(self):
        self.cargo.cargo_write(START_STRIP_SYNC_START)
        # icons = []
        tiles = []

        data = bytes([])
        for x in self.tiles:
            # icons.append(x['icon'])
            tile = bytes([])
            tile += x['guid'].bytes_le
            tile += struct.pack("<I", x['order'])
            tile += struct.pack("<I", x['theme_color'])
            tile += struct.pack("<H", len(x['name']))
            tile += struct.pack("<H", x['settings_mask'])
            tile += MsftBandParser.serialize_text(x['name'], 30)
            tiles.append(tile)
        # data = b''.join(icons)
        data += struct.pack("<I", len(tiles))
        data += b''.join(tiles)

        result = self.cargo.cargo_write_with_data(
            SET_TILES, data, struct.pack("<I", len(tiles))
        )
        self.cargo.cargo_write(START_STRIP_SYNC_END)
        return result

    def request_tiles(self, icons=False):
        max_tiles = self.get_max_tile_capacity()
        response_size = 88 * max_tiles + 4
        command = GET_TILES_NO_IMAGES

        if icons:
            response_size += max_tiles * 1024
            command = GET_TILES

        result, tiles = self.cargo.cargo_read(
            command, response_size)
        tile_data = b"".join(tiles)

        tile_list = []
        tile_icons = []

        begin = 0

        if icons:
            for i in range(0, max_tiles):
                tile_icons.append(tile_data[begin:begin+1024])
                begin += 1024

        # first 4 bytes are tile count
        tile_count = struct.unpack("<I", tile_data[begin:begin+4])[0]
        begin += 4
        i = 0

        # while there are tiles
        while i < tile_count:
            # get guuid
            guid = uuid.UUID(bytes_le=tile_data[begin:begin+16])
            order = struct.unpack("<I", tile_data[begin+16:begin+20])[0]
            theme_color = struct.unpack("<I", tile_data[begin+20:begin+24])[0]
            name_length = struct.unpack("<H", tile_data[begin+24:begin+26])[0]
            settings_mask = struct.unpack(
                "<H", tile_data[begin+26:begin+28]
            )[0]

            # get tile name
            if name_length:
                name = MsftBandParser.bytes_to_text(
                    tile_data[begin+28:begin+80])
            else:
                name = ''

            # append tile to list
            tile_list.append({
                "guid": guid,
                "order": order,
                "theme_color": theme_color,
                "name_length": name_length,
                "settings_mask": settings_mask,
                "name": name,
                "icon": tile_icons[i] if icons else None
            })

            # move to next tile
            begin += 88
            i += 1
        self.tiles = tile_list

    def send_notification(self, notification):
        self.cargo.cargo_write_with_data(
            CARGO_NOTIFICATION, notification.serialize()
        )

    def get_firmware_version(self):
        result, info = self.cargo.cargo_read(CORE_GET_VERSION, 19*3)
        info = b''.join(info)
        self.version = DeviceVersion()

        offset = 0
        for i in range(0, 3):
            fw_version = FirmwareVersion.deserialize(info[offset:offset+19])
            if fw_version.app_name == '1BL':
                self.version.bootloader = fw_version
            elif fw_version.app_name == '2UP':
                self.version.updater = fw_version
            elif fw_version.app_name == 'App':
                self.version.application = fw_version
            offset += 19
        return self.version

    def get_api_version(self):
        result, info = self.cargo.cargo_read(CORE_GET_API_VERSION, 4)
        version = struct.unpack('I', info[0])[0]
        return version

    def get_running_firmware_app(self):
        """
        Returns what mode Band is running in.
        - OneBL - Bootloader
        - TwoUp - Updater
        - App - Regular mode
        - UpApp - Probably also Updater (?)
        """
        result, info = self.cargo.cargo_read(CORE_WHO_AM_I, 1)
        app = struct.unpack('B', info[0])[0]
        return FirmwareApp(app)

    def check_firmware_sdk_bit(self, platform, reserved):
        arguments = struct.pack('BBH', int(platform), int(reserved), 3)
        self.cargo.cargo_write_with_data(CORE_SDK_CHECK, arguments)
