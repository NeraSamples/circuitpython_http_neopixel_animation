# SPDX-FileCopyrightText: Copyright 2023 Neradoc, https://neradoc.me
# SPDX-License-Identifier: MIT
import asyncio
import board
import json
import mdns
import microcontroller
import random
import socketpool
import time
import wifi
import os
from collections import OrderedDict

from digitalio import DigitalInOut, Pull
from analogio import AnalogIn
from micropython import const
from rainbowio import colorwheel

from adafruit_httpserver.server import HTTPServer
from adafruit_httpserver.response import HTTPResponse
from adafruit_httpserver.mime_type import MIMEType

PORT = 8000
ROOT = "/www"
MDNS_HOST_NAME = "bibliolights"

################################################################
# pins
PIN_STRIP = board.PIR_SENSE  # A2
STATUS_PIXEL_NUMBER = 5  # 1

ENABLE_PROXIMITY_SENSOR = False
if ENABLE_PROXIMITY_SENSOR:
    PIN_POLLOLU_EN = board.A2  # A6
    PIN_POLLOLU_IN = board.A3  # A5

################################################################
# number of pixels
NPIXELS = 30
# how long do we stay in "close" proximity mode (seconds)
DUREE_PROCHE = 10
# brightness in minimum mode (normal mode)
LUMINOSITE_MIN = 0.40
# brightness in maximum mode (proximity mode)
LUMINOSITE_MAX = 0.75
# minimum brightness for status LED
LUMINOSITE_STATUS_MIN = 0.3
# maximum brightness for status LED
LUMINOSITE_STATUS_MAX = LUMINOSITE_MAX
# animation speed/delay
PIX_DELAY = 0.05

# weight of the previous proximity value
NOLDS = const(2)
# delay between the proximity sensor enable and distance read
POL_DELAY = 0.025
POL_COOLDOWN = 0.2
# proximity detection distance (higher = closer)
DISTANCE_LIMIT = const(5500)

# default color
current_color = 0x00FFFF

################################################################
# NVM configuration
################################################################
import microcontroller

# [R, G, B, id_anim]
mem = microcontroller.nvm[0:4]
id_anim = mem[0]

# FF = never setup
if id_anim != 0xFF:
    current_color = int.from_bytes(mem[1:4], "big")
else:
    id_anim = 0


def save_mem():
    microcontroller.nvm[0] = id_anim
    microcontroller.nvm[1:4] = current_color.to_bytes(3, "big")


############################################################################
# Helpers
############################################################################


class Box:
    """Box class, to share a value between tasks by reference."""

    def __init__(self, value):
        self.value = value


############################################################################
# neopixel strip
############################################################################

import neopixel
from rainbowio import colorwheel

pixels = neopixel.NeoPixel(PIN_STRIP, NPIXELS, auto_write=False)
pixels.brightness = LUMINOSITE_MIN

status = None
if hasattr(board, "NEOPIXEL"):
    import neopixel
    status = neopixel.NeoPixel(board.NEOPIXEL, STATUS_PIXEL_NUMBER)
elif hasattr(board, "DOTSTAR_CLOCK"):
    import adafruit_dotstar
    status = adafruit_dotstar.DotStar(
        board.DOTSTAR_CLOCK, board.DOTSTAR_DATA, STATUS_PIXEL_NUMBER
    )

if status:
    status.brightness = LUMINOSITE_STATUS_MIN
    status.fill((0, 0, 0))

################################################################
# setup Pololu
################################################################

# mode constants
MODE_MIN, MODE_MAX = 10, 20


class Pol:
    def __init__(self, pol_en, pol_in):
        self.pol_en = pol_en
        self.pol_in = pol_in
        self.last = 0
        self.value = 0
        self.distance = 0
        self.detected = False

    def read(self):
        return False
        return self.pol_in.value

    def update_proximity_status(self):
        now = time.monotonic()
        # change brightness immediately if we are in proximity
        # and reset timer
        if distance > DISTANCE_LIMIT:
            print("Proche", now)
            self.last = now
            pixels.brightness = LUMINOSITE_MAX
            if status:
                status.brightness = LUMINOSITE_MAX
            self.detected = True
        # wait for DUREE_PROCHE before considering we got away
        elif self.last and (now - self.last) > DUREE_PROCHE:
            print("Fini", now)
            self.last = 0
            pixels.brightness = LUMINOSITE_MIN
            if status:
                status.brightness = LUMINOSITE_STATUS_MIN
            self.detected = False

    async def detection_loop(self, running_mode):
        while True:
            # fire up the proximity sensor and wait for the read delay
            self.pol_en.value = True
            await asyncio.sleep(POL_DELAY)

            # modify the new value with the old one based on weight
            val = proximity.read()
            distance = (NOLDS * self.value + val) / (NOLDS + 1)
            self.value = self.distance
            self.pol_en.value = False

            # distance has been measured, update the detection status
            self.update_proximity_status()
            if self.detected:
                running_mode.value = MODE_MAX
            else:
                running_mode.value = MODE_MIN

            # don't detect all the time, have a little cooldown
            await asyncio.sleep(POL_COOLDOWN)


def setup_proximity_sensor():
    pol_en = DigitalInOut(PIN_POLLOLU_EN)
    pol_en.switch_to_output()
    pol_in = AnalogIn(PIN_POLLOLU_IN)
    return Pol(pol_en, pol_in)


if ENABLE_PROXIMITY_SENSOR:
    proximity = setup_proximity_sensor()

################################################################
# setup animations
################################################################

from adafruit_led_animation.group import AnimationGroup
from adafruit_led_animation.color import *
from adafruit_led_animation import helper
from animation_cometschase import CometsChase

# one strip configured as 2 lines starting from the center
stripette = helper.PixelMap.horizontal_lines(
    pixels,
    2,
    NPIXELS // 2,
    helper.vertical_strip_gridmap(NPIXELS // 2, alternating=True),
)

anim_comet_chase = CometsChase(
    stripette,
    speed=PIX_DELAY,
    color=current_color,
    size=10,
    spacing=0,
    reverse=True,
)

anim_alternative = CometsChase(
    stripette,
    speed=PIX_DELAY,
    color=current_color,
    size=5,
    spacing=5,
    reverse=False,
)

animations = OrderedDict([
    ("comet_chase", anim_comet_chase),
    ("alternative", anim_alternative),
])
animation_keys = list(animations.keys())

current_animation_name = "comet_chase"
current_animation = animations[current_animation_name]

def set_animation(animation):
    global current_animation_name, current_animation, id_anim

    if isinstance(animation, str) and animation in animations:
        pos = animation_keys.index(animation)
    elif isinstance(animation, int):
        pos = animation % len(animations)

    if pos == -1:
        print("Can't set animation", animation)
    else:
        current_animation_name = animation_keys[pos]
        current_animation = animations[current_animation_name]
        id_anim = pos

set_animation(id_anim)

################################################################
# color
################################################################


def set_color(color):
    global current_color
    current_color = color
    current_animation.color = current_color
    if status:
        status.fill(current_color)


if status:
    status.fill(current_color)

################################################################
# watchdog
################################################################

# from microcontroller import watchdog as doggy
# from watchdog import WatchDogMode
# doggy.timeout = 10 # Set a timeout in seconds
# doggy.mode = WatchDogMode.RESET

import supervisor
import microcontroller


def maybe_reset():
    print("This is were you reset")
    # if not supervisor.runtime.usb_connected:
    # 	pixels.fill(current_color)
    # 	ble.stop_advertising()
    # 	for connection in ble.connections:
    # 		connection.disconnect()
    # 	time.sleep(2)
    # 	microcontroller.reset()
    pixels.fill(current_color)
    time.sleep(2)


############################################################################
# wifi
############################################################################

if not wifi.radio.connected:
    wifi.radio.connect(
        os.getenv("WIFI_SSID"),
        os.getenv("WIFI_PASSWORD")
    )
print(f"Listening on http://{wifi.radio.ipv4_address}:{PORT}")

pool = socketpool.SocketPool(wifi.radio)
server = HTTPServer(pool, root_path=ROOT)

############################################################################
# server routes and app logic
############################################################################

wheel_colors = [tuple(colorwheel(64 * x).to_bytes(3, "big")) for x in range(4)]


def limit(val):
    try:
        return min(255, max(0, int(val)))
    except ValueError:
        return 0


@server.route("/color")
def base(request):
    global current_animation, current_animation_name
    animation = request.query_params.get("animation", "")
    if animation in animations:
        set_animation(animation)

    print(request.query_params)
    color = request.query_params.get("color", "").replace("#", "")
    if color:
        try:
            color = int(color, 16)
            if color < 0 or color > 0xFFFFFF:
                raise ValueError("Color value out of range")
            print(f"Color: {color:06X}")
            set_color(color)
        except ValueError as er:
            print(f"Color Error: {er}")

    save_mem()

    with HTTPResponse(request, content_type=MIMEType.TYPE_HTML) as response:
        response.send("ok")


@server.route("/getdata")
def getdata(request):
    data = {
        "color": f"{current_color:06X}",
        "animation": current_animation_name,
        "animations": list(animations.keys()),
    }
    with HTTPResponse(request, content_type=MIMEType.TYPE_JSON) as response:
        response.send(json.dumps(data))


# start server
IP_ADDRESS = wifi.radio.ipv4_address or wifi.radio.ipv4_address_ap
server.start(host=str(IP_ADDRESS), port=PORT)


async def web_server():
    while True:
        server.poll()
        await asyncio.sleep(0.01)


############################################################################
# MDNS
############################################################################

mdnserv = mdns.Server(wifi.radio)
mdnserv.hostname = MDNS_HOST_NAME
mdnserv.advertise_service(service_type="_http", protocol="_tcp", port=PORT)

############################################################################
# Loops
############################################################################


async def main():
    running_mode = Box(MODE_MIN)
    # start polling th server
    asyncio.create_task(web_server())
    if ENABLE_PROXIMITY_SENSOR:
        asyncio.create_task(proximity.detection_loop(running_mode))

    while True:
        # play animation depending on the mode
        if running_mode.value == MODE_MAX:
            current_animation.animate(show=False)
            # overlay white pixels during MAX mode
            for i in range(0, NPIXELS, 4):
                pixels[i] = (100, 100, 100)
            pixels.show()
        else:
            # play normal in min mode
            current_animation.animate()

        if status:
            status.fill(colorwheel(time.monotonic() * 100))

        # print(time.monotonic(),end="\r")
        await asyncio.sleep(0.01)


asyncio.run(main())
