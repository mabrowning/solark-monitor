#!/usr/bin/env python3
# coding: utf-8
import asyncio
import logging
import concurrent.futures
#logging.basicConfig(level=logging.DEBUG)

from pymodbus.client.asynchronous.async_io import (
    AsyncioModbusSerialClient,
    ModbusClientProtocol
)
from pymodbus.transaction import ModbusAsciiFramer, ModbusRtuFramer
from pymodbus.factory import ClientDecoder
import websockets

from asyncio_multisubscriber_queue import MultisubscriberQueue

import json
import numpy as np
import time
import datetime

SN01='0211020950'
UNIT=0x1

class InverterProtocol(ModbusClientProtocol):
    def __init__(self, *args, **kwargs):
        kwargs["framer"] = ModbusRtuFramer(ClientDecoder())
        super().__init__(*args, **kwargs)
    def __str__(self):
        return "Inverter"

class Inverter(AsyncioModbusSerialClient):
    def __init__(self, *args, **kwargs):
        kwargs["protocol_class"] = InverterProtocol
        kwargs["timeout"] = 0.5
        super().__init__(*args, **kwargs)

    async def read(self, address, count, dtype=np.uint16):
        return np.array(
            (await self.protocol.read_holding_registers(
                address=address, count=count, unit=UNIT
            )).registers,
            dtype=dtype
        )

    async def serial(self):
        bserial = await self.read(3, 5)
        return "".join(map(chr, bserial.view(np.uint8)))

    async def datapoint(self):
        registers = np.zeros(200, dtype=np.int16)
        registers[166:179] = await self.read(166,179-166)
        registers[184:191] = await self.read(184,191-184)
        datapoint = {
            "load_power_in": registers[166],
            "grid_power": registers[169],
            "limit_power": registers[172],
            "inv_power": registers[175],
            "load_power": registers[178],
            "batt_soc": registers[184],
            "dc_power_pv": registers[186] + registers[187],
            "batt_power": registers[190]
        }
        return datapoint

def combine_fields(datapoints):
    fields = {}
    for field in [
        "dc_power_pv",
        "batt_power",

        "load_power_in",
        "grid_power",
        "limit_power",
        "inv_power",
        "load_power",
    ]:
        val = 0
        for datapoint in datapoints:
            val += datapoint[field]
        fields[field] = int(val)
    fields["batt_soc"] = int(datapoints[0]["batt_soc"])
    fields["house_power"] = fields["limit_power"] - fields["grid_power"]
    return fields

async def read_and_publish(invs, callbacks):
    logging.debug("Start read_and_publish")
    try:
        datapoints = await asyncio.gather(*[inv.datapoint() for inv in invs])
    except concurrent.futures.TimeoutError:
        logging.debug("Inverter read timeout")
        return
    datapoint = combine_fields(datapoints)
    for callback in callbacks:
        logging.debug("DataPoint: "+str(datapoint))
        await callback(datapoint)
        # asyncio.ensure_future(callback(datapoint))
    logging.debug("Done read_and_publish")

def main():
    loop = asyncio.get_event_loop()
    c1 = Inverter(port='/dev/ttyUSB0', loop=loop)
    c2 = Inverter(port='/dev/ttyUSB1', loop=loop)

    async def main_loop():
        # connect and associate c1/c2 (unnecesary?)
        nonlocal c1, c2
        await asyncio.gather(c1.connect(), c2.connect())
        if await c1.serial() != SN01:
            c1, c2 = c2, c1

        # WebSocket server
        mqueue = MultisubscriberQueue()
        async def serve(websocket, path):
            channel = mqueue.subscribe()
            logging.debug("Client Connected: " + str(id(channel)))
            async for data in channel:
                try:
                    logging.debug("Sending: " + str(id(channel)))
                    await websocket.send(json.dumps(data))
                except websockets.exceptions.ConnectionClosed:
                    logging.debug("Disconnect: " + str(id(channel)))
                    return
        await websockets.serve(serve, '0.0.0.0', 8765)

        print("Connected and serving. Entering main loop")
        callbacks = [mqueue.put]
        while True:
            # run no more frequently than once per second
            await asyncio.gather(
                read_and_publish((c1, c2), callbacks),
                asyncio.sleep(1)
            )

    loop.run_until_complete(main_loop())

if __name__ == "__main__":
    main()
