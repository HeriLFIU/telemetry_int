"""INTManager module."""
import asyncio
from collections import defaultdict
from datetime import datetime

from pyof.v0x04.controller2switch.table_mod import Table

from kytos.core.controller import Controller
from kytos.core.events import KytosEvent
from napps.kytos.telemetry_int import utils
from napps.kytos.telemetry_int import settings
from kytos.core import log
import napps.kytos.telemetry_int.kytos_api_helper as api
from napps.kytos.telemetry_int.managers.flow_builder import FlowBuilder
from kytos.core.common import EntityStatus

from napps.kytos.telemetry_int.exceptions import (
    EVCNotFound,
    EVCHasINT,
    EVCHasNoINT,
    ProxyPortStatusNotUP,
    ProxyPortSameSourceIntraEVC
)


class INTManager:

    """INTManager encapsulates and aggregates telemetry-related functionalities."""

    def __init__(self, controller: Controller) -> None:
        """INTManager."""
        self.controller = controller
        self.flow_builder = FlowBuilder()

    async def disable_int(self, evcs: dict[str, dict], force=False) -> None:
        """Disable INT on EVCs.

        evcs is a dict of prefetched EVCs from mef_eline based on evc_ids.

        The force bool option, if True, will bypass the following:

        1 - EVC not found
        2 - EVC doesn't have INT

        """
        self._validate_disable_evcs(evcs, force)

        log.info(f"Disabling INT on EVC ids: {list(evcs.keys())}, force: {force}")

        stored_flows = await api.get_stored_flows(
            [utils.get_cookie(evc_id, settings.INT_COOKIE_PREFIX) for evc_id in evcs]
        )

        metadata = {
            "telemetry": {
                "enabled": False,
                "status": "DOWN",
                "status_reason": ["disabled"],
                "status_updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            }
        }
        await asyncio.gather(
            self.remove_int_flows(stored_flows),
            api.add_evcs_metadata(evcs, metadata, force),
        )

    async def enable_int(self, evcs: dict[str, dict], force=False) -> None:
        """Enable INT on EVCs.

        evcs is a dict of prefetched EVCs from mef_eline based on evc_ids.

        The force bool option, if True, will bypass the following:

        1 - EVC already has INT
        2 - ProxyPort isn't UP
        Other cases won't be bypassed since at the point it won't have the data needed.

        """
        evcs = self._validate_map_enable_evcs(evcs, force)

        log.info(f"Enabling INT on EVC ids: {list(evcs.keys())}, force: {force}")

        stored_flows = self.flow_builder.build_int_flows(
            evcs,
            await utils.get_found_stored_flows(
                [
                    utils.get_cookie(evc_id, settings.MEF_COOKIE_PREFIX)
                    for evc_id in evcs
                ]
            ),
        )

        metadata = {
            "telemetry": {
                "enabled": True,
                "status": "UP",
                "status_reason": [],
                "status_updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            }
        }
        await asyncio.gather(
            self.install_int_flows(stored_flows), api.add_evcs_metadata(evcs, metadata)
        )

    def _validate_disable_evcs(
        self,
        evcs: dict[str, dict],
        force=False,
    ) -> None:
        """Validate disable EVCs."""
        for evc_id, evc in evcs.items():
            if not evc and not force:
                raise EVCNotFound(evc_id)
            if not utils.has_int_enabled(evc) and not force:
                raise EVCHasNoINT(evc_id)

    def _validate_intra_evc_different_proxy_ports(self, evc: dict) -> None:
        """Validate that an intra EVC is using different proxy ports.

        If the same proxy port is used on both UNIs, of one the sink/pop related matches
        would ended up being overwritten since they'd be the same. Currently, an
        external loop will have unidirectional flows matching in the lower (source)
        port number.
        """
        pp_a = evc["uni_a"].get("proxy_port")
        pp_z = evc["uni_z"].get("proxy_port")
        if any(
            (
                not utils.is_intra_switch_evc(evc),
                pp_a is None,
                pp_z is None,
            )
        ):
            return
        if pp_a.source != pp_z.source:
            return

        raise ProxyPortSameSourceIntraEVC(
            evc["id"], "intra EVC UNIs must use different proxy ports"
        )

    def _validate_map_enable_evcs(
        self,
        evcs: dict[str, dict],
        force=False,
    ) -> dict[str, dict]:
        """Validate map enabling EVCs.

        This function also maps both uni_a and uni_z dicts with their ProxyPorts, just
        so it can be reused later during provisioning.

        """
        for evc_id, evc in evcs.items():
            if not evc:
                raise EVCNotFound(evc_id)
            if utils.has_int_enabled(evc) and not force:
                raise EVCHasINT(evc_id)

            uni_a, uni_z = utils.get_evc_unis(evc)
            pp_a = utils.get_proxy_port_or_raise(
                self.controller, uni_a["interface_id"], evc_id
            )
            pp_z = utils.get_proxy_port_or_raise(
                self.controller, uni_z["interface_id"], evc_id
            )

            uni_a["proxy_port"], uni_z["proxy_port"] = pp_a, pp_z
            evc["uni_a"], evc["uni_z"] = uni_a, uni_z

            if pp_a.status != EntityStatus.UP and not force:
                dest_id = pp_a.destination.id if pp_a.destination else None
                dest_status = pp_a.status if pp_a.destination else None
                raise ProxyPortStatusNotUP(
                    evc_id,
                    f"proxy_port of {uni_a['interface_id']} isn't UP. "
                    f"source {pp_a.source.id} status {pp_a.source.status}, "
                    f"destination {dest_id} status {dest_status}",
                )
            if pp_z.status != EntityStatus.UP and not force:
                dest_id = pp_z.destination.id if pp_z.destination else None
                dest_status = pp_z.status if pp_z.destination else None
                raise ProxyPortStatusNotUP(
                    evc_id,
                    f"proxy_port of {uni_z['interface_id']} isn't UP."
                    f"source {pp_z.source.id} status {pp_z.source.status}, "
                    f"destination {dest_id} status {dest_status}",
                )

            self._validate_intra_evc_different_proxy_ports(evc)
        return evcs

    async def remove_int_flows(self, stored_flows: dict[int, list[dict]]) -> None:
        """Delete int flows given a prefiltered stored_flows.

        Removal is driven by the stored flows instead of EVC ids and dpids to also
        be able to handle the force mode when an EVC no longer exists. It also follows
        the same pattern that mef_eline currently uses.

        The flows will be batched per dpid based on settings.BATCH_SIZE and will wait
        for settings.BATCH_INTERVAL per batch iteration.

        """
        switch_flows = defaultdict(set)
        for flows in stored_flows.values():
            for flow in flows:
                switch_flows[flow["switch"]].add(flow["flow"]["cookie"])

        for dpid, cookies in switch_flows.items():
            cookie_vals = list(cookies)
            batch_size = settings.BATCH_SIZE
            if batch_size <= 0:
                batch_size = len(cookie_vals)

            for i in range(0, len(cookie_vals), batch_size):
                if i > 0:
                    await asyncio.sleep(settings.BATCH_INTERVAL)
                flows = [
                    {
                        "cookie": cookie,
                        "cookie_mask": int(0xFFFFFFFFFFFFFFFF),
                        "table_id": Table.OFPTT_ALL.value,
                    }
                    for cookie in cookie_vals[i : i + batch_size]
                ]
                event = KytosEvent(
                    "kytos.flow_manager.flows.delete",
                    content={
                        "dpid": dpid,
                        "force": True,
                        "flow_dict": {"flows": flows},
                    },
                )
                await self.controller.buffers.app.aput(event)

    async def install_int_flows(self, stored_flows: dict[int, list[dict]]) -> None:
        """Install INT flow mods.

        The flows will be batched per dpid based on settings.BATCH_SIZE and will wait
        for settings.BATCH_INTERVAL per batch iteration.
        """

        switch_flows = defaultdict(list)
        for flows in stored_flows.values():
            for flow in flows:
                switch_flows[flow["switch"]].append(flow["flow"])

        for dpid, flows in switch_flows.items():
            flow_vals = list(flows)
            batch_size = settings.BATCH_SIZE
            if batch_size <= 0:
                batch_size = len(flow_vals)

            for i in range(0, len(flow_vals), batch_size):
                if i > 0:
                    await asyncio.sleep(settings.BATCH_INTERVAL)
                flows = flow_vals[i : i + batch_size]
                event = KytosEvent(
                    "kytos.flow_manager.flows.install",
                    content={
                        "dpid": dpid,
                        "force": True,
                        "flow_dict": {"flows": flows},
                    },
                )
                await self.controller.buffers.app.aput(event)
