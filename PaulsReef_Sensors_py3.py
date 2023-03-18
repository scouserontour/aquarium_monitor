#!/usr/bin/env python3

##############################################################################
#
# Originally written by Dominic Bolding:  https://github.com/dombold/MyHydroPi
#
# Updated by Paul Cole for The Raspberry Pi - 2019
#
# Website: paulsreef.co.uk
# Contact: paulsreef.co.uk/contact.php
#
# This program is designed to provide the following features and should be run
# initially from the command line so that a couple of configuration errors can
# be tested for and warnings provided on screen:
#
# 1. Read Multiple Sensors - Atlas Scientific Temperature, pH, and
# Electrical Conductivity sensors and save the results to a MySQL database at a
# set interval with a set level of accuracy.
# A reference temperature reading will be set the temperature sensor if connected,
# if not a value of 25C will be applied. This is necessary to ensure accurate
# readings from the other sensors as the liquid being tested changes temperature.
#
# 2. The program will also create the initial database and tables if they do
# not already exist in MySQL.
#
# For Python 3 I have used cymysql module to connect to the database. To add
# the module you need to enter the following commands:
#
# sudo apt-get install python3-pip
# sudo pip3 install cymysql
#
##############################################################################

import io  # used to create file streams
from io import open
import os
import fcntl  # used to access I2C parameters like addresses
import cymysql
import cymysql.cursors

import time  # used for sleep delay and timestamps
import string  # helps parse strings

from datetime import datetime, timedelta

from collections import OrderedDict

import smtplib


class AtlasI2C:
    long_timeout = 1.5  # the timeout needed to query readings and calibrations
    short_timeout = 0.5  # timeout for regular commands
    default_bus = 1  # the default bus for I2C on the newer Raspberry Pis, certain older boards use bus 0
    default_address = 102  # the default address for the temperature sensor
    current_addr = default_address

    def __init__(self, address=default_address, bus=default_bus):
        # open two file streams, one for reading and one for writing
        # the specific I2C channel is selected with bus
        # it is usually 1, except for older revisions where its 0
        # wb and rb indicate binary read and write
        self.file_read = io.open("/dev/i2c-" + str(bus), "rb", buffering=0)
        self.file_write = io.open("/dev/i2c-" + str(bus), "wb", buffering=0)

        # initializes I2C to either a user specified or default address
        self.set_i2c_address(address)

    def set_i2c_address(self, addr):
        # set the I2C communications to the slave specified by the address
        # The commands for I2C dev using the ioctl functions are specified in
        # the i2c-dev.h file from i2c-tools
        I2C_SLAVE = 0x703
        fcntl.ioctl(self.file_read, I2C_SLAVE, addr)
        fcntl.ioctl(self.file_write, I2C_SLAVE, addr)
        self.current_addr = addr

    def write(self, cmd):
        # appends the null character and sends the string over I2C
        cmd += "\00"
        self.file_write.write(cmd.encode("UTF-8"))

    def read(self, num_of_bytes=31):
        # reads a specified number of bytes from I2C, then parses and displays the result
        res = self.file_read.read(num_of_bytes)  # read from the board
        if type(res[0]) is str:  # if python2 read
            response = [i for i in res]
            if ord(response[0]) == 1:  # if the response isn't an error
                # change MSB to 0 for all received characters except the first and get a list of characters
                # NOTE: having to change the MSB to 0 is a glitch in the raspberry pi, and you shouldn't have to do this!
                char_list = list(map(lambda x: chr(ord(x) & ~0x80), list(response[1:])))
                result = "".join(char_list)
                return result.split("\x00")[
                    0
                ]  # convert the char list to a string and returns it

            else:
                return "Error " + str(ord(response[0]))

        else:  # if python3 read
            if res[0] == 1:
                # change MSB to 0 for all received characters except the first and get a list of characters
                # NOTE: having to change the MSB to 0 is a glitch in the raspberry pi, and you shouldn't have to do this!
                char_list = list(map(lambda x: chr(x & ~0x80), list(res[1:])))
                result = "".join(char_list)
                return result.split("\x00")[
                    0
                ]  # convert the char list to a string and returns it

            else:
                return "Error " + str(res[0])

    def query(self, string):
        # write a command to the board, wait the correct timeout, and read the response
        self.write(string)

        # the read and calibration commands require a longer timeout
        if (string.upper().startswith("R")) or (string.upper().startswith("CAL")):
            time.sleep(self.long_timeout)
        elif string.upper().startswith("SLEEP"):
            return "sleep mode"
        else:
            time.sleep(self.short_timeout)

        return self.read()

    def close(self):
        self.file_read.close()
        self.file_write.close()


# Create required database in the MySQL if it doesn't' already exist


def create_database():
    conn = cymysql.connect(servername, username, password)
    curs = conn.cursor()
    curs.execute("SET sql_notes = 0; ")  # Hide Warnings

    curs.execute("CREATE DATABASE IF NOT EXISTS {}".format(dbname))

    curs.execute("SET sql_notes = 1; ")  # Show Warnings
    conn.commit()
    conn.close()
    return


def open_database_connection():
    conn = cymysql.connect(servername, username, password, dbname)
    curs = conn.cursor()
    curs.execute("SET sql_notes = 0; ")  # Hide Warnings

    return conn, curs


def close_database_connection(conn, curs):
    curs.execute("SET sql_notes = 1; ")
    conn.commit()
    conn.close()


def create_sensors_table():
    conn, curs = open_database_connection()

    curs.execute("CREATE TABLE IF NOT EXISTS sensors (timestamp DATETIME);")

    for key, value in list(sensors.items()):
        if value["is_connected"] is True:
            try:
                curs.execute(
                    "ALTER TABLE sensors ADD {} DECIMAL(10,2);".format(value["name"])
                )
            except:
                pass

    close_database_connection(conn, curs)

    return


def remove_unused_sensors():
    conn, curs = open_database_connection()

    for key, value in list(sensors.items()):
        if value["is_connected"] is False:
            try:
                curs.execute("ALTER TABLE sensors DROP {};".format(value["name"]))
            except:
                pass

    close_database_connection(conn, curs)

    return


def search_database():
    conn, curs = open_database_connection()

    curs.execute("SELECT * FROM sensors ORDER BY `timestamp` DESC LIMIT 1;")
    result = curs.fetchall()

    close_database_connection(conn, curs)

    return result


# read and log each sensor if it is set to True in the sensors list


def log_sensor_readings(all_curr_readings):
    # Create a timestamp and store all readings on the MySQL database

    conn, curs = open_database_connection()

    curs.execute("INSERT INTO sensors (timestamp) VALUES(now());")
    curs.execute("SELECT MAX(timestamp) FROM sensors")
    last_timestamp = curs.fetchone()
    last_timestamp = last_timestamp[0].strftime("%Y-%m-%d %H:%M:%S")

    for readings in all_curr_readings:
        try:
            curs.execute(
                ("UPDATE sensors SET {} = {} WHERE timestamp = '{}'").format(
                    readings[0], readings[1], last_timestamp
                )
            )
        except:
            pass

    close_database_connection(conn, curs)

    return


def read_sensors():
    all_curr_readings = []
    ref_temp = 25

    # Get the readings from any Atlas Scientific temperature sensors

    for key, value in list(sensors.items()):
        if value["is_connected"] is True:
            if value["sensor_type"] == "atlas_scientific_temp":
                device = AtlasI2C(value["i2c"])
                sensor_reading = round(float(device.query("R")), value["accuracy"])
                all_curr_readings.append([value["name"], sensor_reading])
                if value["is_ref"] is True:
                    ref_temp = sensor_reading

            else:
                device = AtlasI2C(value["i2c"])
                # Set reference temperature value on the sensor
                device.query("T," + str(ref_temp))

                # Get the readings from any Atlas Scientific Elec Conductivity sensors

                if value["sensor_type"] == "atlas_scientific_ec":
                    sensor_reading = round(float(device.query("R")), value["accuracy"])

                # Get the readings from any other Atlas Scientific sensors

                else:
                    sensor_reading = round(float(device.query("R")), value["accuracy"])
                all_curr_readings.append([value["name"], sensor_reading])

    log_sensor_readings(all_curr_readings)

    return


def notify():
    search_database()
    global email_time
    # To email notifications
    for x in search_database():
        if x[1] < temp_min or x[1] > temp_max:
            if datetime.now() >= email_time:
                with smtplib.SMTP(smtp_server, port) as server:
                    server.sendmail(
                        sender_email,
                        receiver_email,
                        message.format("temperature", x[1], "C"),
                    )
                    server.quit()
                    email_time = datetime.now() + timedelta(seconds=email_delay)

        if x[2] < ph_min or x[2] > ph_max:
            if datetime.now() >= email_time:
                with smtplib.SMTP(smtp_server, port) as server:
                    server.sendmail(
                        sender_email, receiver_email, message.format("pH", x[2], " ")
                    )
                    server.quit()
                    email_time = datetime.now() + timedelta(seconds=email_delay)

        if x[3] < sal_min or x[3] > sal_max:
            if datetime.now() >= email_time:
                with smtplib.SMTP(smtp_server, port) as server:
                    server.sendmail(
                        sender_email,
                        receiver_email,
                        message.format("salinity", x[3], " ppt"),
                    )
                    server.quit()
                    email_time = datetime.now() + timedelta(seconds=email_delay)
        else:
            email_time = datetime.now()

    return email_time


# Configuration Settings

# Define the sensor names, what sensors are connected, the sensor type, the
# atlas scientific sensor I2C addresses and define a primary temperature sensor.
# In the case shown below that would be "atlas_sensor_1".
# This is the sensor that is in the liquid that is being sampled and is used
# as a reference by the other sensors. If there are no temperature sensors
# connected a default value of 25C will be applied.


sensors = OrderedDict(
    [
        (
            "atlas_sensor_1",
            {  # Atlas Scientific Temp Sensor
                "sensor_type": "atlas_scientific_temp",
                "name": "Temp",
                "is_connected": True,
                "is_ref": True,
                "i2c": 102,
                "accuracy": 2,
            },
        ),
        (
            "atlas_sensor_2",
            {  # pH Atlas Scientific Sensor
                "sensor_type": "atlas_scientific",
                "name": "pH",
                "is_connected": True,
                "is_ref": False,
                "i2c": 99,
                "accuracy": 2,
            },
        ),
        (
            "atlas_sensor_3",
            {  # ORP Atlas Scientific Sensor
                "sensor_type": "atlas_scientific",
                "name": "ORP",
                "is_connected": False,
                "is_ref": False,
                "i2c": 98,
                "accuracy": 0,
            },
        ),
        (
            "atlas_sensor_4",
            {  # Atlas Scientific EC Sensor
                "sensor_type": "atlas_scientific_ec",
                "name": "Salinity",
                "is_connected": True,
                "is_ref": False,
                "i2c": 100,
                "accuracy": 2,
            },
        ),
    ]
)

# Define MySQL database login settings

servername = "localhost"
username = "username"
password = "password"
dbname = "Aquarium_Monitor"

# Define SMTP email settings
smtp_server = "smtp.email.com"
port = 25
sender_email = "sender@email.com"
receiver_email = "receiver@email.com"
message = """\
Subject: Reef Alert!!!

Warning: Reef {} is {}{}"""

# Define notification settings

email_time = datetime.now()
email_delay = 21600
temp_min = 24
temp_max = 28
ph_min = 7.6
ph_max = 8.8
sal_min = 30
sal_max = 37

#################
#               #
# Main Program  #
#               #
#################


# Build/Remove MySQL Database Entries

create_database()
create_sensors_table()
remove_unused_sensors()

while True:  # Repeat the code indefinitely
    read_sensors()
    notify()
    time.sleep(300)
