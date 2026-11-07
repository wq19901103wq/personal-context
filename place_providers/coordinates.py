"""Conversions at provider boundaries; stored recommendation points use WGS84."""

from math import cos, pi, sin, sqrt


def gcj_offset(lat, lon):
    if not (72.004 <= lon <= 137.8347 and .8293 <= lat <= 55.8271):
        return 0., 0.
    x, y = lon - 105., lat - 35.
    a = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*sqrt(abs(x))
    b = 300 + x + 2*y + .1*x*x + .1*x*y + .2*sqrt(abs(x))
    wave = (20*sin(6*x*pi) + 20*sin(2*x*pi)) * 2 / 3
    a += wave + (20*sin(y*pi) + 40*sin(y*pi/3))*2/3 + (160*sin(y*pi/12) + 320*sin(y*pi/30))*2/3
    b += wave + (20*sin(x*pi) + 40*sin(x*pi/3))*2/3 + (150*sin(x*pi/12) + 300*sin(x*pi/30))*2/3
    rad = lat * pi / 180
    magic = 1 - .00669342162296594323 * sin(rad)**2
    return (a * 180 / ((6378245 * (1 - .00669342162296594323) / (magic * sqrt(magic))) * pi),
            b * 180 / ((6378245 / sqrt(magic)) * cos(rad) * pi))


def wgs84_to_gcj02(lat, lon):
    dy, dx = gcj_offset(lat, lon)
    return lat + dy, lon + dx


def gcj02_to_wgs84(lat, lon):
    result_lat, result_lon = lat, lon
    for _ in range(3):
        dy, dx = gcj_offset(result_lat, result_lon)
        result_lat, result_lon = lat - dy, lon - dx
    return result_lat, result_lon
