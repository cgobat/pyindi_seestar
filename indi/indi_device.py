
import asyncio
import datetime as dt
from lxml import etree
from pyindi.device import device as IDevice, stdio


class MultiDevice(IDevice):
    """Subclass of pyINDI device class that provides support for multiple INDI devices in one.
    
    This is mostly just a matter of removing enforcement of checks against `self._devname`, and
    adding a `dev` argument to `.IDMessage()` in order to specify which device is emitting the
    message.

    This class also slightly changes `.start()` and `.astart()` to actually use the optional `loop`
    argument provided on instantiation. The superclass assigns that argument to the `.mainloop`
    attribute but then doesn't ever do anything with it.
    """

    def __init__(self, devices=[], config=None, loop=None):
        super().__init__(config=config, loop=loop)
        self.device_names: "list[str]" = devices

    def IUFind(self, name, device, group=None):
        """Look up a vector property by device & name"""

        for p in self.props:
            if p.name == name and p.device == device:
                if group is not None:
                    if p.group == group:
                        return p
                else:
                    return p
        else:
            raise KeyError(f"No property with device {device!r} and name {name!r} found.")

    def IDDef(self, prop, msg=None):
        """Register a property internally"""

        if prop.device not in self.device_names:
            raise ValueError(f"INDI prop {prop.name}'s device '{prop.device}' does not match any"
                             f"of this object's devices: {self.device_names}")
        if prop not in self.props:
            self.props.append(prop)
        # Send it to the indiserver
        self.outq.put_nowait((etree.tostring(prop.Def(msg), pretty_print=True)))
    
    def IDMessage(self, msg: str, timestamp = None, msgtype: str = "INFO", dev = None):
        """Send a message to the client"""

        if isinstance(timestamp, dt.datetime):
            timestamp = timestamp.isoformat()
        elif timestamp is None:
            timestamp = dt.datetime.now().isoformat()
        xml = etree.Element("message", attrib={"message": f"[{msgtype}] {msg}",
                                               "timestamp": timestamp,
                                               "device": dev or self._devname})
        self.outq.put_nowait(etree.tostring(xml))

    def start(self):
        """Like superclass' `.start()` but uses existing `self.mainloop` attribute if present."""
        
        if self.mainloop is None:
            self.mainloop = asyncio.get_event_loop()
        self.reader, self.writer = self.mainloop.run_until_complete(stdio())
        self.running = True
        future = asyncio.gather(
            self.run(),
            self.toindiserver(),
            self.repeat_queuer()
        )
        self.mainloop.run_until_complete(future)

    async def astart(self, *tasks):
        """Like superclass' `.astart()` but uses existing `self.mainloop` attribute if present."""

        if self.mainloop is None:
            self.mainloop = asyncio.get_running_loop()
        self.reader, self.writer = await stdio()
        self.running = True
        future = asyncio.gather(
            self.run(),
            self.toindiserver(),
            self.repeat_queuer(),
            *tasks
        )
        await future