import os
import time
from datetime import datetime

import pytz
from tzlocal import get_localzone as tzlocal_get_localzone

from paasta_tools.api import client


def get_localzone():
    if "TZ" in os.environ:
        return pytz.timezone(os.environ["TZ"])
    else:
        return tzlocal_get_localzone()


def get_service_autoscale_pause_time(cluster):
    api = client.get_paasta_api_client(cluster=cluster, http_res=True)
    if not api:
        print("Could not connect to paasta api. Maybe you misspelled the cluster?")
        return 1
    pause_time, http = api.service_autoscaler.get_service_autoscaler_pause().result()
    if http.status_code == 500:
        print("Could not connect to zookeeper server")
        return 2

    pause_time = float(pause_time)
    if pause_time < time.time():
        print("Service autoscaler is not paused")
    else:
        local_tz = get_localzone()
        paused_readable = local_tz.localize(
            datetime.fromtimestamp(pause_time)
        ).strftime("%F %H:%M:%S %Z")
        print(f"Service autoscaler is paused until {paused_readable}")

    return 0


def update_service_autoscale_pause_time(cluster, mins):
    api = client.get_paasta_api_client(cluster=cluster, http_res=True)
    if not api:
        print("Could not connect to paasta api. Maybe you misspelled the cluster?")
        return 1
    body = {"minutes": mins}
    res, http = api.service_autoscaler.update_service_autoscaler_pause(
        json_body=body
    ).result()
    if http.status_code == 500:
        print("Could not connect to zookeeper server")
        return 2

    print(f"Service autoscaler is paused for {mins}")
    return 0


def delete_service_autoscale_pause_time(cluster):
    api = client.get_paasta_api_client(cluster=cluster, http_res=True)
    if not api:
        print("Could not connect to paasta api. Maybe you misspelled the cluster?")
        return 1
    res, http = api.service_autoscaler.delete_service_autoscaler_pause().result()
    if http.status_code == 500:
        print("Could not connect to zookeeper server")
        return 2

    print("Service autoscaler is unpaused")
    return 0