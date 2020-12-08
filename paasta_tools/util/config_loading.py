import copy
import difflib
import glob
import json
import logging
import math
import os
import re
import socket
from fnmatch import fnmatch
from functools import lru_cache
from typing import Any
from typing import cast
from typing import Collection
from typing import Dict
from typing import FrozenSet
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Set
from typing import Tuple
from typing import TypeVar
from typing import Union

import service_configuration_lib

import paasta_tools.cli.fsm
from paasta_tools.deployment import BranchDictV2
from paasta_tools.util.cache import time_cache
from paasta_tools.util.config_types import AwsEbsVolume
from paasta_tools.util.config_types import Constraint
from paasta_tools.util.config_types import DeployBlacklist
from paasta_tools.util.config_types import DeployWhitelist
from paasta_tools.util.config_types import DockerParameter
from paasta_tools.util.config_types import DockerVolume
from paasta_tools.util.config_types import ExpectedSlaveAttributes
from paasta_tools.util.config_types import IdToClusterAutoscalingResourcesDict
from paasta_tools.util.config_types import InstanceConfigDict
from paasta_tools.util.config_types import KubeCustomResourceDict
from paasta_tools.util.config_types import LocalRunConfig
from paasta_tools.util.config_types import LogReaderConfig
from paasta_tools.util.config_types import LogWriterConfig
from paasta_tools.util.config_types import MarathonConfigDict
from paasta_tools.util.config_types import NoConfigurationForServiceError
from paasta_tools.util.config_types import PaastaNativeConfig
from paasta_tools.util.config_types import PersistentVolume
from paasta_tools.util.config_types import PoolToResourcePoolSettingsDict
from paasta_tools.util.config_types import RemoteRunConfig
from paasta_tools.util.config_types import SecretVolume
from paasta_tools.util.config_types import SparkRunConfig
from paasta_tools.util.config_types import SystemPaastaConfigDict
from paasta_tools.util.config_types import UnsafeDeployBlacklist
from paasta_tools.util.config_types import UnsafeDeployWhitelist
from paasta_tools.util.config_types import UnstringifiedConstraint
from paasta_tools.util.const import AUTO_SOACONFIG_SUBDIR
from paasta_tools.util.const import DEFAULT_CPU_BURST_ADD
from paasta_tools.util.const import DEFAULT_CPU_PERIOD
from paasta_tools.util.const import DEFAULT_DOCKERCFG_LOCATION
from paasta_tools.util.const import DEFAULT_SOA_CONFIGS_GIT_URL
from paasta_tools.util.const import DEFAULT_SOA_DIR
from paasta_tools.util.const import DEFAULT_SYNAPSE_HAPROXY_URL_FORMAT
from paasta_tools.util.const import DEPLOY_PIPELINE_NON_DEPLOY_STEPS
from paasta_tools.util.const import INSTANCE_TYPES
from paasta_tools.util.const import PATH_TO_SYSTEM_PAASTA_CONFIG_DIR
from paasta_tools.util.const import SPACER
from paasta_tools.util.deep_merge import deep_merge_dictionaries
from paasta_tools.util.names import compose_job_id
from paasta_tools.util.names import InvalidJobNameError


log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


_SortDictsT = TypeVar("_SortDictsT", bound=Mapping)


def sort_dicts(dcts: Iterable[_SortDictsT]) -> List[_SortDictsT]:
    def key(dct: _SortDictsT) -> Tuple:
        return tuple(sorted(dct.items()))

    return sorted(dcts, key=key)


class InvalidInstanceConfig(Exception):
    pass


class NoDockerImageError(Exception):
    pass


class PaastaNotConfiguredError(Exception):
    pass


def safe_deploy_blacklist(input: UnsafeDeployBlacklist) -> DeployBlacklist:
    return [(t, l) for t, l in input]


def safe_deploy_whitelist(input: UnsafeDeployWhitelist) -> DeployWhitelist:
    try:
        location_type, allowed_values = input
        return cast(str, location_type), cast(List[str], allowed_values)
    except TypeError:
        return None


def deploy_blacklist_to_constraints(
    deploy_blacklist: DeployBlacklist,
) -> List[Constraint]:
    """Converts a blacklist of locations into marathon appropriate constraints.

    https://mesosphere.github.io/marathon/docs/constraints.html#unlike-operator

    :param blacklist: List of lists of locations to blacklist
    :returns: List of lists of constraints
    """
    constraints: List[Constraint] = []
    for blacklisted_location in deploy_blacklist:
        constraints.append([blacklisted_location[0], "UNLIKE", blacklisted_location[1]])

    return constraints


def deploy_whitelist_to_constraints(
    deploy_whitelist: DeployWhitelist,
) -> List[Constraint]:
    """Converts a whitelist of locations into marathon appropriate constraints

    https://mesosphere.github.io/marathon/docs/constraints.html#like-operator

    :param deploy_whitelist: List of lists of locations to whitelist
    :returns: List of lists of constraints
    """
    if deploy_whitelist is not None:
        (region_type, regions) = deploy_whitelist
        regionstr = "|".join(regions)

        return [[region_type, "LIKE", regionstr]]
    return []


def _reorder_docker_volumes(volumes: List[DockerVolume]) -> List[DockerVolume]:
    deduped = {
        v["containerPath"].rstrip("/") + v["hostPath"].rstrip("/"): v for v in volumes
    }.values()
    return sort_dicts(deduped)


def get_service_docker_registry(
    service: str,
    soa_dir: str = DEFAULT_SOA_DIR,
    system_config: Optional["SystemPaastaConfig"] = None,
) -> str:
    if service is None:
        raise NotImplementedError('"None" is not a valid service')
    service_configuration = service_configuration_lib.read_service_configuration(
        service, soa_dir
    )
    try:
        return service_configuration["docker_registry"]
    except KeyError:
        if not system_config:
            system_config = load_system_paasta_config()
        return system_config.get_system_docker_registry()


# TODO: pass docker registry and move to util.names
def build_docker_image_name(service: str) -> str:
    """docker-paasta.yelpcorp.com:443 is the URL for the Registry where PaaSTA
    will look for your images.

    :returns: a sanitized-for-Jenkins (s,/,-,g) version of the
    service's path in git. E.g. For github.yelpcorp.com:services/foo the
    docker image name is docker_registry/services-foo.
    """
    docker_registry_url = get_service_docker_registry(service)
    name = f"{docker_registry_url}/services-{service}"
    return name


def build_docker_tag(service: str, upstream_git_commit: str) -> str:
    """Builds the DOCKER_TAG string

    upstream_git_commit is the SHA that we're building. Usually this is the
    tip of origin/master.
    """
    tag = "{}:paasta-{}".format(build_docker_image_name(service), upstream_git_commit)
    return tag


def get_paasta_branch(cluster: str, instance: str) -> str:
    return SPACER.join((cluster, instance))


def get_git_sha_from_dockerurl(docker_url: str, long: bool = False) -> str:
    """ We encode the sha of the code that built a docker image *in* the docker
    url. This function takes that url as input and outputs the sha.
    """
    parts = docker_url.split("/")
    parts = parts[-1].split("-")
    sha = parts[-1]
    return sha if long else sha[:8]


def get_code_sha_from_dockerurl(docker_url: str) -> str:
    """ code_sha is hash extracted from docker url prefixed with "git", short
    hash is used because it's embedded in marathon app names and there's length
    limit.
    """
    try:
        git_sha = get_git_sha_from_dockerurl(docker_url, long=False)
        return "git%s" % git_sha
    except Exception:
        return "gitUNKNOWN"


def get_pipeline_config(service: str, soa_dir: str = DEFAULT_SOA_DIR) -> List[Dict]:
    service_configuration = service_configuration_lib.read_service_configuration(
        service, soa_dir
    )
    return service_configuration.get("deploy", {}).get("pipeline", [])


def is_deploy_step(step: str) -> bool:
    """
    Returns true if the given step deploys to an instancename
    Returns false if the step is a predefined step-type, e.g. itest or command-*
    """
    return not (
        (step in DEPLOY_PIPELINE_NON_DEPLOY_STEPS) or (step.startswith("command-"))
    )


def get_pipeline_deploy_groups(
    service: str, soa_dir: str = DEFAULT_SOA_DIR
) -> List[str]:
    pipeline_steps = [step["step"] for step in get_pipeline_config(service, soa_dir)]
    return [step for step in pipeline_steps if is_deploy_step(step)]


# For mypy typing
InstanceConfig_T = TypeVar("InstanceConfig_T", bound="InstanceConfig")


class InstanceConfig:
    config_filename_prefix: str

    def __init__(
        self,
        cluster: str,
        instance: str,
        service: str,
        config_dict: InstanceConfigDict,
        branch_dict: Optional[BranchDictV2],
        soa_dir: str = DEFAULT_SOA_DIR,
    ) -> None:
        self.config_dict = config_dict
        self.branch_dict = branch_dict
        self.cluster = cluster
        self.instance = instance
        self.service = service
        self.soa_dir = soa_dir
        self._job_id = compose_job_id(service, instance)
        config_interpolation_keys = ("deploy_group",)
        interpolation_facts = self.__get_interpolation_facts()
        for key in config_interpolation_keys:
            if (
                key in self.config_dict
                and self.config_dict[key] is not None  # type: ignore
            ):
                self.config_dict[key] = self.config_dict[key].format(  # type: ignore
                    **interpolation_facts
                )

    def __repr__(self) -> str:
        return "{!s}({!r}, {!r}, {!r}, {!r}, {!r}, {!r})".format(
            self.__class__.__name__,
            self.service,
            self.instance,
            self.cluster,
            self.config_dict,
            self.branch_dict,
            self.soa_dir,
        )

    def __get_interpolation_facts(self) -> Dict[str, str]:
        return {
            "cluster": self.cluster,
            "instance": self.instance,
            "service": self.service,
        }

    def get_cluster(self) -> str:
        return self.cluster

    def get_instance(self) -> str:
        return self.instance

    def get_service(self) -> str:
        return self.service

    @property
    def job_id(self) -> str:
        return self._job_id

    def get_docker_registry(
        self, system_paasta_config: Optional["SystemPaastaConfig"] = None
    ) -> str:
        return get_service_docker_registry(
            self.service, self.soa_dir, system_config=system_paasta_config
        )

    def get_branch(self) -> str:
        return get_paasta_branch(
            cluster=self.get_cluster(), instance=self.get_instance()
        )

    def get_deploy_group(self) -> str:
        return self.config_dict.get("deploy_group", self.get_branch())

    def get_team(self) -> str:
        return self.config_dict.get("monitoring", {}).get("team", None)

    def get_mem(self) -> float:
        """Gets the memory required from the service's configuration.

        Defaults to 4096 (4G) if no value specified in the config.

        :returns: The amount of memory specified by the config, 4096 if not specified"""
        mem = self.config_dict.get("mem", 4096)
        return mem

    def get_mem_swap(self) -> str:
        """Gets the memory-swap value. This value is passed to the docker
        container to ensure that the total memory limit (memory + swap) is the
        same value as the 'mem' key in soa-configs. Note - this value *has* to
        be >= to the mem key, so we always round up to the closest MB and add
        additional 64MB for the docker executor (See PAASTA-12450).
        """
        mem = self.get_mem()
        mem_swap = int(math.ceil(mem + 64))
        return "%sm" % mem_swap

    def get_cpus(self) -> float:
        """Gets the number of cpus required from the service's configuration.

        Defaults to 1 cpu if no value specified in the config.

        :returns: The number of cpus specified in the config, 1 if not specified"""
        cpus = self.config_dict.get("cpus", 1)
        return cpus

    def get_cpu_burst_add(self) -> float:
        """Returns the number of additional cpus a container is allowed to use.
        Defaults to DEFAULT_CPU_BURST_ADD"""
        return self.config_dict.get("cpu_burst_add", DEFAULT_CPU_BURST_ADD)

    def get_cpu_period(self) -> float:
        """The --cpu-period option to be passed to docker
        Comes from the cfs_period_us configuration option

        :returns: The number to be passed to the --cpu-period docker flag"""
        return self.config_dict.get("cfs_period_us", DEFAULT_CPU_PERIOD)

    def get_cpu_quota(self) -> float:
        """Gets the --cpu-quota option to be passed to docker

        Calculation: (cpus + cpus_burst_add) * cfs_period_us

        :returns: The number to be passed to the --cpu-quota docker flag"""
        cpu_burst_add = self.get_cpu_burst_add()
        return (self.get_cpus() + cpu_burst_add) * self.get_cpu_period()

    def get_extra_docker_args(self) -> Dict[str, str]:
        return self.config_dict.get("extra_docker_args", {})

    def get_cap_add(self) -> Iterable[DockerParameter]:
        """Get the --cap-add options to be passed to docker
        Generated from the cap_add configuration option, which is a list of
        capabilities.

        Example configuration: {'cap_add': ['IPC_LOCK', 'SYS_PTRACE']}

        :returns: A generator of cap_add options to be passed as --cap-add flags"""
        for value in self.config_dict.get("cap_add", []):
            yield {"key": "cap-add", "value": f"{value}"}

    def get_cap_drop(self) -> Iterable[DockerParameter]:
        """Generates --cap-drop options to be passed to docker by default, which
        makes them not able to perform special privilege escalation stuff
        https://docs.docker.com/engine/reference/run/#runtime-privilege-and-linux-capabilities
        """
        caps = [
            "SETPCAP",
            "MKNOD",
            "AUDIT_WRITE",
            "CHOWN",
            "NET_RAW",
            "DAC_OVERRIDE",
            "FOWNER",
            "FSETID",
            "KILL",
            "SETGID",
            "SETUID",
            "NET_BIND_SERVICE",
            "SYS_CHROOT",
            "SETFCAP",
        ]
        for cap in caps:
            yield {"key": "cap-drop", "value": cap}

    def format_docker_parameters(
        self,
        with_labels: bool = True,
        system_paasta_config: Optional["SystemPaastaConfig"] = None,
    ) -> List[DockerParameter]:
        """Formats extra flags for running docker.  Will be added in the format
        `["--%s=%s" % (e['key'], e['value']) for e in list]` to the `docker run` command
        Note: values must be strings

        :param with_labels: Whether to build docker parameters with or without labels
        :returns: A list of parameters to be added to docker run"""
        parameters: List[DockerParameter] = [
            {"key": "memory-swap", "value": self.get_mem_swap()},
            {"key": "cpu-period", "value": "%s" % int(self.get_cpu_period())},
            {"key": "cpu-quota", "value": "%s" % int(self.get_cpu_quota())},
        ]
        if self.use_docker_disk_quota(system_paasta_config=system_paasta_config):
            parameters.append(
                {
                    "key": "storage-opt",
                    "value": f"size={int(self.get_disk() * 1024 * 1024)}",
                }
            )
        if with_labels:
            parameters.extend(
                [
                    {"key": "label", "value": "paasta_service=%s" % self.service},
                    {"key": "label", "value": "paasta_instance=%s" % self.instance},
                ]
            )
        extra_docker_args = self.get_extra_docker_args()
        if extra_docker_args:
            for key, value in extra_docker_args.items():
                parameters.extend([{"key": key, "value": value}])
        parameters.extend(self.get_cap_add())
        parameters.extend(self.get_docker_init())
        parameters.extend(self.get_cap_drop())
        return parameters

    def use_docker_disk_quota(
        self, system_paasta_config: Optional["SystemPaastaConfig"] = None
    ) -> bool:
        if system_paasta_config is None:
            system_paasta_config = load_system_paasta_config()
        return system_paasta_config.get_enforce_disk_quota()

    def get_docker_init(self) -> Iterable[DockerParameter]:
        return [{"key": "init", "value": "true"}]

    def get_disk(self, default: float = 1024) -> float:
        """Gets the amount of disk space in MiB required from the service's configuration.

        Defaults to 1024 (1GiB) if no value is specified in the config.

        :returns: The amount of disk space specified by the config, 1024 MiB if not specified"""
        disk = self.config_dict.get("disk", default)
        return disk

    def get_gpus(self) -> Optional[int]:
        """Gets the number of gpus required from the service's configuration.

        Default to None if no value is specified in the config.

        :returns: The number of gpus specified by the config, 0 if not specified"""
        gpus = self.config_dict.get("gpus", None)
        return gpus

    def get_container_type(self) -> Optional[str]:
        """Get Mesos containerizer type.

        Default to DOCKER if gpus are not used.

        :returns: Mesos containerizer type, DOCKER or MESOS"""
        if self.get_gpus() is not None:
            container_type = "MESOS"
        else:
            container_type = "DOCKER"
        return container_type

    def get_cmd(self) -> Optional[Union[str, List[str]]]:
        """Get the docker cmd specified in the service's configuration.

        Defaults to None if not specified in the config.

        :returns: A string specified in the config, None if not specified"""
        return self.config_dict.get("cmd", None)

    def get_instance_type(self) -> Optional[str]:
        return getattr(self, "config_filename_prefix", None)

    def get_env_dictionary(
        self, system_paasta_config: Optional["SystemPaastaConfig"] = None
    ) -> Dict[str, str]:
        """A dictionary of key/value pairs that represent environment variables
        to be injected to the container environment"""
        env = {
            "PAASTA_SERVICE": self.service,
            "PAASTA_INSTANCE": self.instance,
            "PAASTA_CLUSTER": self.cluster,
            "PAASTA_DEPLOY_GROUP": self.get_deploy_group(),
            "PAASTA_DOCKER_IMAGE": self.get_docker_image(),
            "PAASTA_RESOURCE_CPUS": str(self.get_cpus()),
            "PAASTA_RESOURCE_MEM": str(self.get_mem()),
            "PAASTA_RESOURCE_DISK": str(self.get_disk()),
        }
        if self.get_gpus() is not None:
            env["PAASTA_RESOURCE_GPUS"] = str(self.get_gpus())
        try:
            env["PAASTA_GIT_SHA"] = get_git_sha_from_dockerurl(
                self.get_docker_url(system_paasta_config=system_paasta_config)
            )
        except Exception:
            pass
        team = self.get_team()
        if team:
            env["PAASTA_MONITORING_TEAM"] = team
        instance_type = self.get_instance_type()
        if instance_type:
            env["PAASTA_INSTANCE_TYPE"] = instance_type
        user_env = self.config_dict.get("env", {})
        env.update(user_env)
        return {str(k): str(v) for (k, v) in env.items()}

    def get_env(
        self, system_paasta_config: Optional["SystemPaastaConfig"] = None
    ) -> Dict[str, str]:
        """Basic get_env that simply returns the basic env, other classes
        might need to override this getter for more implementation-specific
        env getting"""
        return self.get_env_dictionary(system_paasta_config=system_paasta_config)

    def get_args(self) -> Optional[List[str]]:
        """Get the docker args specified in the service's configuration.

        If not specified in the config and if cmd is not specified, defaults to an empty array.
        If not specified in the config but cmd is specified, defaults to null.
        If specified in the config and if cmd is also specified, throws an exception. Only one may be specified.

        :param service_config: The service instance's configuration dictionary
        :returns: An array of args specified in the config,
            ``[]`` if not specified and if cmd is not specified,
            otherwise None if not specified but cmd is specified"""
        if self.get_cmd() is None:
            return self.config_dict.get("args", [])
        else:
            args = self.config_dict.get("args", None)
            if args is None:
                return args
            else:
                # TODO validation stuff like this should be moved into a check_*
                raise InvalidInstanceConfig(
                    "Instance configuration can specify cmd or args, but not both."
                )

    def get_monitoring(self) -> Dict[str, Any]:
        """Get monitoring overrides defined for the given instance"""
        return self.config_dict.get("monitoring", {})

    def get_deploy_constraints(
        self,
        blacklist: DeployBlacklist,
        whitelist: DeployWhitelist,
        system_deploy_blacklist: DeployBlacklist,
        system_deploy_whitelist: DeployWhitelist,
    ) -> List[Constraint]:
        """Return the combination of deploy_blacklist and deploy_whitelist
        as a list of constraints.
        """
        return (
            deploy_blacklist_to_constraints(blacklist)
            + deploy_whitelist_to_constraints(whitelist)
            + deploy_blacklist_to_constraints(system_deploy_blacklist)
            + deploy_whitelist_to_constraints(system_deploy_whitelist)
        )

    def get_deploy_blacklist(self) -> DeployBlacklist:
        """The deploy blacklist is a list of lists, where the lists indicate
        which locations the service should not be deployed"""
        return safe_deploy_blacklist(self.config_dict.get("deploy_blacklist", []))

    def get_deploy_whitelist(self) -> DeployWhitelist:
        """The deploy whitelist is a tuple of (location_type, [allowed value, allowed value, ...]).
        To have tasks scheduled on it, a host must be covered by the deploy whitelist (if present) and not excluded by
        the deploy blacklist."""

        return safe_deploy_whitelist(self.config_dict.get("deploy_whitelist"))

    def get_docker_image(self) -> str:
        """Get the docker image name (with tag) for a given service branch from
        a generated deployments.json file."""
        if self.branch_dict is not None:
            return self.branch_dict["docker_image"]
        else:
            return ""

    def get_docker_url(
        self, system_paasta_config: Optional["SystemPaastaConfig"] = None
    ) -> str:
        """Compose the docker url.
        :returns: '<registry_uri>/<docker_image>'
        """
        registry_uri = self.get_docker_registry(
            system_paasta_config=system_paasta_config
        )
        docker_image = self.get_docker_image()
        if not docker_image:
            raise NoDockerImageError(
                "Docker url not available because there is no docker_image"
            )
        docker_url = f"{registry_uri}/{docker_image}"
        return docker_url

    def get_desired_state(self) -> str:
        """Get the desired state (either 'start' or 'stop') for a given service
        branch from a generated deployments.json file."""
        if self.branch_dict is not None:
            return self.branch_dict["desired_state"]
        else:
            return "start"

    def get_force_bounce(self) -> Optional[str]:
        """Get the force_bounce token for a given service branch from a generated
        deployments.json file. This is a token that, when changed, indicates that
        the instance should be recreated and bounced, even if no other
        parameters have changed. This may be None or a string, generally a
        timestamp.
        """
        if self.branch_dict is not None:
            return self.branch_dict["force_bounce"]
        else:
            return None

    def check_cpus(self) -> Tuple[bool, str]:
        cpus = self.get_cpus()
        if cpus is not None:
            if not isinstance(cpus, (float, int)):
                return (
                    False,
                    'The specified cpus value "%s" is not a valid float or int.' % cpus,
                )
        return True, ""

    def check_mem(self) -> Tuple[bool, str]:
        mem = self.get_mem()
        if mem is not None:
            if not isinstance(mem, (float, int)):
                return (
                    False,
                    'The specified mem value "%s" is not a valid float or int.' % mem,
                )
        return True, ""

    def check_disk(self) -> Tuple[bool, str]:
        disk = self.get_disk()
        if disk is not None:
            if not isinstance(disk, (float, int)):
                return (
                    False,
                    'The specified disk value "%s" is not a valid float or int.' % disk,
                )
        return True, ""

    def check_security(self) -> Tuple[bool, str]:
        security = self.config_dict.get("security")
        if security is None:
            return True, ""

        inbound_firewall = security.get("inbound_firewall")
        outbound_firewall = security.get("outbound_firewall")

        if inbound_firewall is None and outbound_firewall is None:
            return True, ""

        if inbound_firewall is not None and inbound_firewall not in (
            "allow",
            "reject",
        ):
            return (
                False,
                'Unrecognized inbound_firewall value "%s"' % inbound_firewall,
            )

        if outbound_firewall is not None and outbound_firewall not in (
            "block",
            "monitor",
        ):
            return (
                False,
                'Unrecognized outbound_firewall value "%s"' % outbound_firewall,
            )

        unknown_keys = set(security.keys()) - {
            "inbound_firewall",
            "outbound_firewall",
        }
        if unknown_keys:
            return (
                False,
                'Unrecognized items in security dict of service config: "%s"'
                % ",".join(unknown_keys),
            )

        return True, ""

    def check_dependencies_reference(self) -> Tuple[bool, str]:
        dependencies_reference = self.config_dict.get("dependencies_reference")
        if dependencies_reference is None:
            return True, ""

        dependencies = self.config_dict.get("dependencies")
        if dependencies is None:
            return (
                False,
                'dependencies_reference "%s" declared but no dependencies found'
                % dependencies_reference,
            )

        if dependencies_reference not in dependencies:
            return (
                False,
                'dependencies_reference "%s" not found in dependencies dictionary'
                % dependencies_reference,
            )

        return True, ""

    def check(self, param: str) -> Tuple[bool, str]:
        check_methods = {
            "cpus": self.check_cpus,
            "mem": self.check_mem,
            "security": self.check_security,
            "dependencies_reference": self.check_dependencies_reference,
            "deploy_group": self.check_deploy_group,
        }
        check_method = check_methods.get(param)
        if check_method is not None:
            return check_method()
        else:
            return (
                False,
                'Your service config specifies "%s", an unsupported parameter.' % param,
            )

    def validate(
        self,
        params: List[str] = [
            "cpus",
            "mem",
            "security",
            "dependencies_reference",
            "deploy_group",
        ],
    ) -> List[str]:
        error_msgs = []
        for param in params:
            check_passed, check_msg = self.check(param)
            if not check_passed:
                error_msgs.append(check_msg)
        return error_msgs

    def check_deploy_group(self) -> Tuple[bool, str]:
        deploy_group = self.get_deploy_group()
        if deploy_group is not None:
            pipeline_deploy_groups = get_pipeline_deploy_groups(
                service=self.service, soa_dir=self.soa_dir
            )
            if deploy_group not in pipeline_deploy_groups:
                return (
                    False,
                    f"{self.service}.{self.instance} uses deploy_group {deploy_group}, but it is not deploy.yaml",
                )  # noqa: E501
        return True, ""

    def get_extra_volumes(self) -> List[DockerVolume]:
        """Extra volumes are a specially formatted list of dictionaries that should
        be bind mounted in a container The format of the dictionaries should
        conform to the `Mesos container volumes spec
        <https://mesosphere.github.io/marathon/docs/native-docker.html>`_"""
        return self.config_dict.get("extra_volumes", [])

    def get_aws_ebs_volumes(self) -> List[AwsEbsVolume]:
        return self.config_dict.get("aws_ebs_volumes", [])

    def get_secret_volumes(self) -> List[SecretVolume]:
        return self.config_dict.get("secret_volumes", [])

    def get_role(self) -> Optional[str]:
        """Which mesos role of nodes this job should run on.
        """
        return self.config_dict.get("role")

    def get_pool(self) -> str:
        """Which pool of nodes this job should run on. This can be used to mitigate noisy neighbors, by putting
        particularly noisy or noise-sensitive jobs into different pools.

        This is implemented with an attribute "pool" on each mesos slave and by adding a constraint or node selector.

        Eventually this may be implemented with Mesos roles, once a framework can register under multiple roles.

        :returns: the "pool" attribute in your config dict, or the string "default" if not specified."""
        return self.config_dict.get("pool", "default")

    def get_pool_constraints(self) -> List[Constraint]:
        pool = self.get_pool()
        return [["pool", "LIKE", pool]]

    def get_constraints(self) -> Optional[List[Constraint]]:
        return stringify_constraints(self.config_dict.get("constraints", None))

    def get_extra_constraints(self) -> List[Constraint]:
        return stringify_constraints(self.config_dict.get("extra_constraints", []))

    def get_net(self) -> str:
        """
        :returns: the docker networking mode the container should be started with.
        """
        return self.config_dict.get("net", "bridge")

    def get_volumes(self, system_volumes: Sequence[DockerVolume]) -> List[DockerVolume]:
        volumes = list(system_volumes) + list(self.get_extra_volumes())
        return _reorder_docker_volumes(volumes)

    def get_persistent_volumes(self) -> Sequence[PersistentVolume]:
        return self.config_dict.get("persistent_volumes", [])

    def get_dependencies_reference(self) -> Optional[str]:
        """Get the reference to an entry in dependencies.yaml

        Defaults to None if not specified in the config.

        :returns: A string specified in the config, None if not specified"""
        return self.config_dict.get("dependencies_reference")

    def get_dependencies(self) -> Optional[Dict]:
        """Get the contents of the dependencies_dict pointed to by the dependency_reference or
        'main' if no dependency_reference exists

        Defaults to None if not specified in the config.

        :returns: A list of dictionaries specified in the dependencies_dict, None if not specified"""
        dependencies = self.config_dict.get("dependencies")
        if not dependencies:
            return None
        dependency_ref = self.get_dependencies_reference() or "main"
        return dependencies.get(dependency_ref)

    def get_inbound_firewall(self) -> Optional[str]:
        """Return 'allow', 'reject', or None as configured in security->inbound_firewall
        Defaults to None if not specified in the config

        Setting this to a value other than `allow` is uncommon, as doing so will restrict the
        availability of your service. The only other supported value is `reject` currently,
        which will reject all remaining inbound traffic to the service port after all other rules.

        This option exists primarily for sensitive services that wish to opt into this functionality.

        :returns: A string specified in the config, None if not specified"""
        security = self.config_dict.get("security")
        if not security:
            return None
        return security.get("inbound_firewall")

    def get_outbound_firewall(self) -> Optional[str]:
        """Return 'block', 'monitor', or None as configured in security->outbound_firewall

        Defaults to None if not specified in the config

        :returns: A string specified in the config, None if not specified"""
        security = self.config_dict.get("security")
        if not security:
            return None
        return security.get("outbound_firewall")

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, type(self)):
            return (
                self.config_dict == other.config_dict
                and self.branch_dict == other.branch_dict
                and self.cluster == other.cluster
                and self.instance == other.instance
                and self.service == other.service
            )
        else:
            return False


def stringify_constraint(usc: UnstringifiedConstraint) -> Constraint:
    return [str(x) for x in usc]


def stringify_constraints(
    uscs: Optional[List[UnstringifiedConstraint]],
) -> List[Constraint]:
    if uscs is None:
        return None
    return [stringify_constraint(usc) for usc in uscs]


class SystemPaastaConfig:
    def __init__(self, config: SystemPaastaConfigDict, directory: str) -> None:
        self.directory = directory
        self.config_dict = config

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SystemPaastaConfig):
            return (
                self.directory == other.directory
                and self.config_dict == other.config_dict
            )
        return False

    def __repr__(self) -> str:
        return f"SystemPaastaConfig({self.config_dict!r}, {self.directory!r})"

    def get_zk_hosts(self) -> str:
        """Get the zk_hosts defined in this hosts's cluster config file.
        Strips off the zk:// prefix, if it exists, for use with Kazoo.

        :returns: The zk_hosts specified in the paasta configuration
        """
        try:
            hosts = self.config_dict["zookeeper"]
        except KeyError:
            raise PaastaNotConfiguredError(
                "Could not find zookeeper connection string in configuration directory: %s"
                % self.directory
            )

        # how do python strings not have a method for doing this
        if hosts.startswith("zk://"):
            return hosts[len("zk://") :]
        return hosts

    def get_system_docker_registry(self) -> str:
        """Get the docker_registry defined in this host's cluster config file.

        :returns: The docker_registry specified in the paasta configuration
        """
        try:
            return self.config_dict["docker_registry"]
        except KeyError:
            raise PaastaNotConfiguredError(
                "Could not find docker registry in configuration directory: %s"
                % self.directory
            )

    def get_hacheck_sidecar_volumes(self) -> List[DockerVolume]:
        """Get the hacheck sidecar volumes defined in this host's hacheck_sidecar_volumes config file.

        :returns: The list of volumes specified in the paasta configuration
        """
        try:
            volumes = self.config_dict["hacheck_sidecar_volumes"]
        except KeyError:
            raise PaastaNotConfiguredError(
                "Could not find hacheck_sidecar_volumes in configuration directory: %s"
                % self.directory
            )
        return _reorder_docker_volumes(list(volumes))

    def get_volumes(self) -> Sequence[DockerVolume]:
        """Get the volumes defined in this host's volumes config file.

        :returns: The list of volumes specified in the paasta configuration
        """
        try:
            return self.config_dict["volumes"]
        except KeyError:
            raise PaastaNotConfiguredError(
                "Could not find volumes in configuration directory: %s" % self.directory
            )

    def get_cluster(self) -> str:
        """Get the cluster defined in this host's cluster config file.

        :returns: The name of the cluster defined in the paasta configuration
        """
        try:
            return self.config_dict["cluster"]
        except KeyError:
            raise PaastaNotConfiguredError(
                "Could not find cluster in configuration directory: %s" % self.directory
            )

    def get_dashboard_links(self) -> Mapping[str, Mapping[str, str]]:
        return self.config_dict["dashboard_links"]

    def get_auto_hostname_unique_size(self) -> int:
        """
        We automatically add a ["hostname", "UNIQUE"] constraint to "small" services running in production clusters.
        If there are less than or equal to this number of instances, we consider it small.
        We fail safe and return -1 to avoid adding the ['hostname', 'UNIQUE'] constraint if this value is not defined

        :returns: The integer size of a small service
        """
        return self.config_dict.get("auto_hostname_unique_size", -1)

    def get_auto_config_instance_types_enabled(self) -> Dict[str, bool]:
        return self.config_dict.get("auto_config_instance_types_enabled", {})

    def get_api_endpoints(self) -> Mapping[str, str]:
        return self.config_dict["api_endpoints"]

    def get_enable_client_cert_auth(self) -> bool:
        """
        If enabled present a client certificate from ~/.paasta/pki/<cluster>.crt and ~/.paasta/pki/<cluster>.key
        """
        return self.config_dict.get("enable_client_cert_auth", True)

    def get_enable_nerve_readiness_check(self) -> bool:
        """
        If enabled perform readiness checks on nerve
        """
        return self.config_dict.get("enable_nerve_readiness_check", True)

    def get_enable_envoy_readiness_check(self) -> bool:
        """
        If enabled perform readiness checks on envoy
        """
        return self.config_dict.get("enable_envoy_readiness_check", False)

    def get_nerve_readiness_check_script(self) -> List[str]:
        return self.config_dict.get(
            "nerve_readiness_check_script", ["/check_smartstack_up.sh"]
        )

    def get_envoy_readiness_check_script(self) -> List[str]:
        return self.config_dict.get(
            "envoy_readiness_check_script", ["/check_proxy_up.sh", "--enable-envoy"]
        )

    def get_envoy_nerve_readiness_check_script(self) -> List[str]:
        return self.config_dict.get(
            "envoy_nerve_readiness_check_script",
            ["/check_proxy_up.sh", "--enable-smartstack", "--enable-envoy"],
        )

    def get_enforce_disk_quota(self) -> bool:
        """
        If enabled, add `--storage-opt size=SIZE` arg to `docker run` calls,
        enforcing the disk quota as a result.

        Please note that this should be enabled only for a suported environment
        (which at the moment is only `overlay2` driver backed by `XFS`
        filesystem mounted with `prjquota` option) otherwise Docker will fail
        to start.
        """
        return self.config_dict.get("enforce_disk_quota", False)

    def get_auth_certificate_ttl(self) -> str:
        """
        How long to request for ttl on auth certificates. Note that this maybe limited
        by policy in Vault
        """
        return self.config_dict.get("auth_certificate_ttl", "11h")

    def get_pki_backend(self) -> str:
        """
        The Vault pki backend to use for issueing certificates
        """
        return self.config_dict.get("pki_backend", "paastaca")

    def get_fsm_template(self) -> str:
        fsm_path = os.path.dirname(paasta_tools.cli.fsm.__file__)
        template_path = os.path.join(fsm_path, "template")
        return self.config_dict.get("fsm_template", template_path)

    def get_log_writer(self) -> LogWriterConfig:
        """Get the log_writer configuration out of global paasta config

        :returns: The log_writer dictionary.
        """
        try:
            return self.config_dict["log_writer"]
        except KeyError:
            raise PaastaNotConfiguredError(
                "Could not find log_writer in configuration directory: %s"
                % self.directory
            )

    def get_log_reader(self) -> LogReaderConfig:
        """Get the log_reader configuration out of global paasta config

        :returns: the log_reader dictionary.
        """
        try:
            return self.config_dict["log_reader"]
        except KeyError:
            raise PaastaNotConfiguredError(
                "Could not find log_reader in configuration directory: %s"
                % self.directory
            )

    def get_metrics_provider(self) -> Optional[str]:
        """Get the metrics_provider configuration out of global paasta config

        :returns: A string identifying the metrics_provider
        """
        deployd_metrics_provider = self.config_dict.get("deployd_metrics_provider")
        if deployd_metrics_provider is not None:
            return deployd_metrics_provider
        return self.config_dict.get("metrics_provider")

    def get_deployd_worker_failure_backoff_factor(self) -> int:
        """Get the factor for calculating exponential backoff when a deployd worker
        fails to bounce a service

        :returns: An integer
        """
        return self.config_dict.get("deployd_worker_failure_backoff_factor", 30)

    def get_deployd_maintenance_polling_frequency(self) -> int:
        """Get the frequency in seconds that the deployd maintenance watcher should
        poll mesos's api for new draining hosts

        :returns: An integer
        """
        return self.config_dict.get("deployd_maintenance_polling_frequency", 30)

    def get_deployd_startup_oracle_enabled(self) -> bool:
        """This controls whether deployd will add all services that need a bounce on
        startup. Generally this is desirable behavior. If you are performing a bounce
        of *all* services you will want to disable this.

        :returns: A boolean
        """
        return self.config_dict.get("deployd_startup_oracle_enabled", True)

    def get_deployd_max_service_instance_failures(self) -> int:
        """Determines how many times a service instance entry in deployd's queue
        can fail before it will be removed from the queue.

        :returns: An integer
        """
        return self.config_dict.get("deployd_max_service_instance_failures", 20)

    def get_sensu_host(self) -> str:
        """Get the host that we should send sensu events to.

        :returns: the sensu_host string, or localhost if not specified.
        """
        return self.config_dict.get("sensu_host", "localhost")

    def get_sensu_port(self) -> int:
        """Get the port that we should send sensu events to.

        :returns: the sensu_port value as an integer, or 3030 if not specified.
        """
        return int(self.config_dict.get("sensu_port", 3030))

    def get_dockercfg_location(self) -> str:
        """Get the location of the dockerfile, as a URI.

        :returns: the URI specified, or file:///root/.dockercfg if not specified.
        """
        return self.config_dict.get("dockercfg_location", DEFAULT_DOCKERCFG_LOCATION)

    def get_synapse_port(self) -> int:
        """Get the port that haproxy-synapse exposes its status on. Defaults to 3212.

        :returns: the haproxy-synapse status port."""
        return int(self.config_dict.get("synapse_port", 3212))

    def get_default_synapse_host(self) -> str:
        """Get the default host we should interrogate for haproxy-synapse state.

        :returns: A hostname that is running haproxy-synapse."""
        return self.config_dict.get("synapse_host", "localhost")

    def get_synapse_haproxy_url_format(self) -> str:
        """Get a format string for the URL to query for haproxy-synapse state. This format string gets two keyword
        arguments, host and port. Defaults to "http://{host:s}:{port:d}/;csv;norefresh".

        :returns: A format string for constructing the URL of haproxy-synapse's status page."""
        return self.config_dict.get(
            "synapse_haproxy_url_format", DEFAULT_SYNAPSE_HAPROXY_URL_FORMAT
        )

    def get_service_discovery_providers(self) -> Dict[str, Any]:
        return self.config_dict.get("service_discovery_providers", {})

    def get_cluster_autoscaling_resources(self) -> IdToClusterAutoscalingResourcesDict:
        return self.config_dict.get("cluster_autoscaling_resources", {})

    def get_cluster_autoscaling_draining_enabled(self) -> bool:
        """ Enable mesos maintenance mode and trigger draining of instances before the
        autoscaler terminates the instance.

        :returns A bool"""
        return self.config_dict.get("cluster_autoscaling_draining_enabled", True)

    def get_cluster_autoscaler_max_increase(self) -> float:
        """ Set the maximum increase that the cluster autoscaler can make in each run

        :returns A float"""
        return self.config_dict.get("cluster_autoscaler_max_increase", 0.2)

    def get_cluster_autoscaler_max_decrease(self) -> float:
        """ Set the maximum decrease that the cluster autoscaler can make in each run

        :returns A float"""
        return self.config_dict.get("cluster_autoscaler_max_decrease", 0.1)

    def get_maintenance_resource_reservation_enabled(self) -> bool:
        """ Enable un/reserving of resources when we un/drain a host in mesos maintenance
        *and* after tasks are killed in setup_marathon_job etc.

        :returns A bool"""
        return self.config_dict.get("maintenance_resource_reservation_enabled", True)

    def get_cluster_boost_enabled(self) -> bool:
        """ Enable the cluster boost. Note that the boost only applies to the CPUs.
        If the boost is toggled on here but not configured, it will be transparent.

        :returns A bool: True means cluster boost is enabled."""
        return self.config_dict.get("cluster_boost_enabled", False)

    def get_resource_pool_settings(self) -> PoolToResourcePoolSettingsDict:
        return self.config_dict.get("resource_pool_settings", {})

    def get_cluster_fqdn_format(self) -> str:
        """Get a format string that constructs a DNS name pointing at the paasta masters in a cluster. This format
        string gets one parameter: cluster. Defaults to 'paasta-{cluster:s}.yelp'.

        :returns: A format string for constructing the FQDN of the masters in a given cluster."""
        return self.config_dict.get("cluster_fqdn_format", "paasta-{cluster:s}.yelp")

    def get_marathon_servers(self) -> List[MarathonConfigDict]:
        return self.config_dict.get("marathon_servers", [])

    def get_previous_marathon_servers(self) -> List[MarathonConfigDict]:
        return self.config_dict.get("previous_marathon_servers", [])

    def get_local_run_config(self) -> LocalRunConfig:
        """Get the local-run config

        :returns: The local-run job config dictionary"""
        return self.config_dict.get("local_run_config", {})

    def get_remote_run_config(self) -> RemoteRunConfig:
        """Get the remote-run config

        :returns: The remote-run system_paasta_config dictionary"""
        return self.config_dict.get("remote_run_config", {})

    def get_spark_run_config(self) -> SparkRunConfig:
        """Get the spark-run config

        :returns: The spark-run system_paasta_config dictionary"""
        return self.config_dict.get("spark_run_config", {})

    def get_paasta_native_config(self) -> PaastaNativeConfig:
        return self.config_dict.get("paasta_native", {})

    def get_mesos_cli_config(self) -> Dict:
        """Get the config for mesos-cli

        :returns: The mesos cli config
        """
        return self.config_dict.get("mesos_config", {})

    def get_monitoring_config(self) -> Dict:
        """Get the monitoring config

        :returns: the monitoring config dictionary"""
        return self.config_dict.get("monitoring_config", {})

    def get_deploy_blacklist(self) -> DeployBlacklist:
        """Get global blacklist. This applies to all services
        in the cluster

        :returns: The blacklist
        """
        return safe_deploy_blacklist(self.config_dict.get("deploy_blacklist", []))

    def get_deploy_whitelist(self) -> DeployWhitelist:
        """Get global whitelist. This applies to all services
        in the cluster

        :returns: The whitelist
        """

        return safe_deploy_whitelist(self.config_dict.get("deploy_whitelist"))

    def get_expected_slave_attributes(self) -> ExpectedSlaveAttributes:
        """Return a list of dictionaries, representing the expected combinations of attributes in this cluster. Used for
        calculating the default routing constraints."""
        return self.config_dict.get("expected_slave_attributes")

    def get_security_check_command(self) -> Optional[str]:
        """Get the script to be executed during the security-check build step

        :return: The name of the file
        """
        return self.config_dict.get("security_check_command", None)

    def get_deployd_number_workers(self) -> int:
        """Get the number of workers to consume deployment q

        :return: integer
        """
        return self.config_dict.get("deployd_number_workers", 4)

    def get_deployd_big_bounce_deadline(self) -> float:
        """Get the amount of time in the future to set the deadline when enqueuing instances for SystemPaastaConfig
        changes.

        :return: float
        """

        return float(
            self.config_dict.get("deployd_big_bounce_deadline", 7 * 24 * 60 * 60)
        )

    def get_deployd_startup_bounce_deadline(self) -> float:
        """Get the amount of time in the future to set the deadline when enqueuing instances on deployd startup.

        :return: float
        """

        return float(
            self.config_dict.get("deployd_startup_bounce_deadline", 7 * 24 * 60 * 60)
        )

    def get_deployd_log_level(self) -> str:
        """Get the log level for paasta-deployd

        :return: string name of python logging level, e.g. INFO, DEBUG etc.
        """
        return self.config_dict.get("deployd_log_level", "INFO")

    def get_deployd_use_zk_queue(self) -> bool:
        return self.config_dict.get("deployd_use_zk_queue", True)

    def get_hacheck_sidecar_image_url(self) -> str:
        """Get the docker image URL for the hacheck sidecar container"""
        return self.config_dict.get(
            "hacheck_sidecar_image_url",
            "docker-paasta.yelpcorp.com:443/hacheck-k8s-sidecar",
        )

    def get_register_k8s_pods(self) -> bool:
        """Enable registration of k8s services in nerve"""
        return self.config_dict.get("register_k8s_pods", False)

    def get_kubernetes_custom_resources(self) -> Sequence[KubeCustomResourceDict]:
        """List of custom resources that should be synced by setup_kubernetes_cr """
        return self.config_dict.get("kubernetes_custom_resources", [])

    def get_kubernetes_use_hacheck_sidecar(self) -> bool:
        return self.config_dict.get("kubernetes_use_hacheck_sidecar", True)

    def get_register_marathon_services(self) -> bool:
        """Enable registration of marathon services in nerve"""
        return self.config_dict.get("register_marathon_services", True)

    def get_register_native_services(self) -> bool:
        """Enable registration of native paasta services in nerve"""
        return self.config_dict.get("register_native_services", False)

    def get_taskproc(self) -> Dict:
        return self.config_dict.get("taskproc", {})

    def get_disabled_watchers(self) -> List:
        return self.config_dict.get("disabled_watchers", [])

    def get_vault_environment(self) -> Optional[str]:
        """ Get the environment name for the vault cluster
        This must match the environment keys in the secret json files
        used by all services in this cluster"""
        return self.config_dict.get("vault_environment")

    def get_vault_cluster_config(self) -> dict:
        """ Get a map from paasta_cluster to vault ecosystem. We need
        this because not every ecosystem will have its own vault cluster"""
        return self.config_dict.get("vault_cluster_map", {})

    def get_secret_provider_name(self) -> str:
        """ Get the name for the configured secret_provider, used to
        decrypt secrets"""
        return self.config_dict.get("secret_provider", "paasta_tools.secret_providers")

    def get_slack_token(self) -> str:
        """ Get a slack token for slack notifications. Returns None if there is
        none available """
        return self.config_dict.get("slack", {}).get("token", None)

    def get_tron_config(self) -> dict:
        return self.config_dict.get("tron", {})

    def get_clusters(self) -> Sequence[str]:
        return self.config_dict.get("clusters", [])

    def get_supported_storage_classes(self) -> Sequence[str]:
        return self.config_dict.get("supported_storage_classes", [])

    def get_envoy_admin_endpoint_format(self) -> str:
        """ Get the format string for Envoy's admin interface. """
        return self.config_dict.get(
            "envoy_admin_endpoint_format", "http://{host:s}:{port:d}/{endpoint:s}"
        )

    def get_envoy_admin_port(self) -> int:
        """ Get the port that Envoy's admin interface is listening on
        from /etc/services. """
        return socket.getservbyname(
            self.config_dict.get("envoy_admin_domain_name", "envoy-admin")
        )

    def get_pdb_max_unavailable(self) -> Union[str, int]:
        return self.config_dict.get("pdb_max_unavailable", 0)

    def get_boost_regions(self) -> List[str]:
        return self.config_dict.get("boost_regions", [])

    def get_pod_defaults(self) -> Dict[str, Any]:
        return self.config_dict.get("pod_defaults", {})

    def get_ldap_search_base(self) -> str:
        return self.config_dict.get("ldap_search_base", None)

    def get_ldap_search_ou(self) -> str:
        return self.config_dict.get("ldap_search_ou", None)

    def get_ldap_host(self) -> str:
        return self.config_dict.get("ldap_host", None)

    def get_ldap_reader_username(self) -> str:
        return self.config_dict.get("ldap_reader_username", None)

    def get_ldap_reader_password(self) -> str:
        return self.config_dict.get("ldap_reader_password", None)

    def get_default_push_groups(self) -> List:
        return self.config_dict.get("default_push_groups", None)

    def get_git_config(self) -> Dict:
        """Gets git configuration. Includes repo names and their git servers.

        :returns: the git config dict
        """
        return self.config_dict.get(
            "git_config",
            {
                "git_user": "git",
                "repos": {
                    "yelpsoa-configs": {
                        "repo_name": "yelpsoa-configs",
                        "git_server": DEFAULT_SOA_CONFIGS_GIT_URL,
                        "deploy_server": DEFAULT_SOA_CONFIGS_GIT_URL,
                    },
                },
            },
        )

    def get_git_repo_config(self, repo_name: str) -> Dict:
        """Gets the git configuration for a specific repo.

        :returns: the git config dict for a specific repo.
        """
        return self.get_git_config().get("repos", {}).get(repo_name, {})

    def get_hpa_always_uses_external_for_signalfx(self) -> bool:
        return self.config_dict.get("hpa_always_uses_external_for_signalfx", False)


def filter_templates_from_config(config: Dict) -> Dict[str, Any]:
    config = {
        key: value for key, value in config.items() if not key.startswith("_")
    }  # filter templates
    return config or {}


def get_readable_files_in_glob(glob: str, path: str) -> List[str]:
    """
    Returns a sorted list of files that are readable in an input glob by recursively searching a path
    """
    globbed_files = []
    for root, dirs, files in os.walk(path):
        for f in files:
            fn = os.path.join(root, f)
            if os.path.isfile(fn) and os.access(fn, os.R_OK) and fnmatch(fn, glob):
                globbed_files.append(fn)
    return sorted(globbed_files)


def load_all_configs(
    cluster: str, file_prefix: str, soa_dir: str
) -> Mapping[str, Mapping[str, Any]]:
    config_dicts = {}
    for service in os.listdir(soa_dir):
        config_dicts[
            service
        ] = service_configuration_lib.read_extra_service_information(
            service, f"{file_prefix}-{cluster}", soa_dir=soa_dir
        )
    return config_dicts


@lru_cache()
def parse_system_paasta_config(
    file_stats: FrozenSet[Tuple[str, os.stat_result]], path: str
) -> SystemPaastaConfig:
    """Pass in a dictionary of filename -> os.stat_result, and this returns the merged parsed configs"""
    config: SystemPaastaConfigDict = {}
    for filename, _ in file_stats:
        with open(filename) as f:
            config = deep_merge_dictionaries(
                json.load(f), config, allow_duplicate_keys=False
            )
    return SystemPaastaConfig(config, path)


def load_system_paasta_config(
    path: str = PATH_TO_SYSTEM_PAASTA_CONFIG_DIR,
) -> SystemPaastaConfig:
    """
    Reads Paasta configs in specified directory in lexicographical order and deep merges
    the dictionaries (last file wins).
    """
    if not os.path.isdir(path):
        raise PaastaNotConfiguredError(
            "Could not find system paasta configuration directory: %s" % path
        )

    if not os.access(path, os.R_OK):
        raise PaastaNotConfiguredError(
            "Could not read from system paasta configuration directory: %s" % path
        )

    try:
        file_stats = frozenset(
            {
                (fn, os.stat(fn))
                for fn in get_readable_files_in_glob(glob="*.json", path=path)
            }
        )
        return parse_system_paasta_config(file_stats, path)
    except IOError as e:
        raise PaastaNotConfiguredError(
            f"Could not load system paasta config file {e.filename}: {e.strerror}"
        )


def optionally_load_system_paasta_config(
    path: str = PATH_TO_SYSTEM_PAASTA_CONFIG_DIR,
) -> SystemPaastaConfig:
    """
    Tries to load the system paasta config, but will return an empty configuration if not available,
    without raising.
    """
    try:
        return load_system_paasta_config(path=path)
    except PaastaNotConfiguredError:
        return SystemPaastaConfig({}, "")


def load_service_instance_configs(
    service: str, instance_type: str, cluster: str, soa_dir: str = DEFAULT_SOA_DIR,
) -> Dict[str, InstanceConfigDict]:
    conf_file = f"{instance_type}-{cluster}"
    user_configs = service_configuration_lib.read_extra_service_information(
        service, conf_file, soa_dir=soa_dir, deepcopy=False,
    )
    user_configs = filter_templates_from_config(user_configs)
    auto_configs = load_service_instance_auto_configs(
        service, instance_type, cluster, soa_dir
    )
    merged = {}
    for instance_name, user_config in user_configs.items():
        auto_config = auto_configs.get(instance_name, {})
        merged[instance_name] = deep_merge_dictionaries(
            overrides=user_config, defaults=auto_config,
        )
    return merged


def load_service_instance_config(
    service: str,
    instance: str,
    instance_type: str,
    cluster: str,
    soa_dir: str = DEFAULT_SOA_DIR,
) -> InstanceConfigDict:
    if instance.startswith("_"):
        raise InvalidJobNameError(
            f"Unable to load {instance_type} config for {service}.{instance} as instance name starts with '_'"
        )
    conf_file = f"{instance_type}-{cluster}"

    # We pass deepcopy=False here and then do our own deepcopy of the subset of the data we actually care about. Without
    # this optimization, any code that calls load_service_instance_config for every instance in a yaml file is ~O(n^2).
    user_config = copy.deepcopy(
        service_configuration_lib.read_extra_service_information(
            service, conf_file, soa_dir=soa_dir, deepcopy=False
        ).get(instance)
    )
    if user_config is None:
        raise NoConfigurationForServiceError(
            f"{instance} not found in config file {soa_dir}/{service}/{conf_file}.yaml."
        )

    auto_config = load_service_instance_auto_configs(
        service, instance_type, cluster, soa_dir
    ).get(instance, {})
    return deep_merge_dictionaries(overrides=user_config, defaults=auto_config,)


def load_service_instance_auto_configs(
    service: str, instance_type: str, cluster: str, soa_dir: str = DEFAULT_SOA_DIR,
) -> Dict[str, Dict[str, Any]]:
    enabled_types = load_system_paasta_config().get_auto_config_instance_types_enabled()
    conf_file = f"{instance_type}-{cluster}"
    if enabled_types.get(instance_type):
        return service_configuration_lib.read_extra_service_information(
            service,
            f"{AUTO_SOACONFIG_SUBDIR}/{conf_file}",
            soa_dir=soa_dir,
            deepcopy=False,
        )
    else:
        return {}


def read_service_instance_names(
    service: str, instance_type: str, cluster: str, soa_dir: str
) -> Collection[Tuple[str, str]]:
    instance_list = []
    conf_file = f"{instance_type}-{cluster}"
    config = service_configuration_lib.read_extra_service_information(
        service, conf_file, soa_dir=soa_dir, deepcopy=False,
    )
    config = filter_templates_from_config(config)
    if instance_type == "tron":
        for job_name, job in config.items():
            action_names = list(job.get("actions", {}).keys())
            for name in action_names:
                instance = f"{job_name}.{name}"
                instance_list.append((service, instance))
    else:
        for instance in config:
            instance_list.append((service, instance))
    return instance_list


def get_service_instance_list_no_cache(
    service: str,
    cluster: Optional[str] = None,
    instance_type: str = None,
    soa_dir: str = DEFAULT_SOA_DIR,
) -> List[Tuple[str, str]]:
    """Enumerate the instances defined for a service as a list of tuples.

    :param service: The service name
    :param cluster: The cluster to read the configuration for
    :param instance_type: The type of instances to examine: 'marathon', 'tron', or None (default) for both
    :param soa_dir: The SOA config directory to read from
    :returns: A list of tuples of (name, instance) for each instance defined for the service name
    """

    instance_types: Tuple[str, ...]
    if not cluster:
        cluster = load_system_paasta_config().get_cluster()
    if instance_type in INSTANCE_TYPES:
        instance_types = (instance_type,)
    else:
        instance_types = INSTANCE_TYPES

    instance_list: List[Tuple[str, str]] = []
    for srv_instance_type in instance_types:
        instance_list.extend(
            read_service_instance_names(
                service=service,
                instance_type=srv_instance_type,
                cluster=cluster,
                soa_dir=soa_dir,
            )
        )
    log.debug("Enumerated the following instances: %s", instance_list)
    return instance_list


@time_cache(ttl=5)
def get_service_instance_list(
    service: str,
    cluster: Optional[str] = None,
    instance_type: str = None,
    soa_dir: str = DEFAULT_SOA_DIR,
) -> List[Tuple[str, str]]:
    """Enumerate the instances defined for a service as a list of tuples.

    :param service: The service name
    :param cluster: The cluster to read the configuration for
    :param instance_type: The type of instances to examine: 'marathon', 'tron', or None (default) for both
    :param soa_dir: The SOA config directory to read from
    :returns: A list of tuples of (name, instance) for each instance defined for the service name
    """
    return get_service_instance_list_no_cache(
        service=service, cluster=cluster, instance_type=instance_type, soa_dir=soa_dir
    )


def get_services_for_cluster(
    cluster: str = None, instance_type: str = None, soa_dir: str = DEFAULT_SOA_DIR
) -> List[Tuple[str, str]]:
    """Retrieve all services and instances defined to run in a cluster.

    :param cluster: The cluster to read the configuration for
    :param instance_type: The type of instances to examine: 'marathon', 'tron', or None (default) for both
    :param soa_dir: The SOA config directory to read from
    :returns: A list of tuples of (service, instance)
    """

    if not cluster:
        cluster = load_system_paasta_config().get_cluster()
    rootdir = os.path.abspath(soa_dir)
    log.debug(
        "Retrieving all service instance names from %s for cluster %s", rootdir, cluster
    )
    instance_list: List[Tuple[str, str]] = []
    for srv_dir in os.listdir(rootdir):
        instance_list.extend(
            get_service_instance_list(srv_dir, cluster, instance_type, soa_dir)
        )
    return instance_list


def suggest_possibilities(
    word: str, possibilities: Iterable[str], max_suggestions: int = 3
) -> str:
    suggestions = cast(
        List[str],
        difflib.get_close_matches(
            word=word, possibilities=set(possibilities), n=max_suggestions
        ),
    )
    if len(suggestions) == 1:
        return f"\nDid you mean: {suggestions[0]}?"
    elif len(suggestions) >= 1:
        return f"\nDid you mean one of: {', '.join(suggestions)}?"
    else:
        return ""


@time_cache(ttl=60)
def validate_service_instance(
    service: str, instance: str, cluster: str, soa_dir: str
) -> str:
    possibilities: List[str] = []
    for instance_type in INSTANCE_TYPES:
        sis = get_service_instance_list(
            service=service,
            cluster=cluster,
            instance_type=instance_type,
            soa_dir=soa_dir,
        )
        if (service, instance) in sis:
            return instance_type
        possibilities.extend(si[1] for si in sis)
    else:
        suggestions = suggest_possibilities(word=instance, possibilities=possibilities)
        raise NoConfigurationForServiceError(
            f"Error: {compose_job_id(service, instance)} doesn't look like it has been configured "
            f"to run on the {cluster} cluster.{suggestions}"
        )


def list_services(soa_dir: str = DEFAULT_SOA_DIR) -> Sequence[str]:
    """Returns a sorted list of all services"""
    return sorted(os.listdir(os.path.abspath(soa_dir)))


def list_all_instances_for_service(
    service: str,
    clusters: Iterable[str] = None,
    instance_type: str = None,
    soa_dir: str = DEFAULT_SOA_DIR,
    cache: bool = True,
) -> Set[str]:
    instances = set()
    if not clusters:
        clusters = list_clusters(service, soa_dir=soa_dir)
    for cluster in clusters:
        if cache:
            si_list = get_service_instance_list(
                service, cluster, instance_type, soa_dir=soa_dir
            )
        else:
            si_list = get_service_instance_list_no_cache(
                service, cluster, instance_type, soa_dir=soa_dir
            )
        for service_instance in si_list:
            instances.add(service_instance[1])
    return instances


def get_soa_cluster_deploy_files(
    service: str = None, soa_dir: str = DEFAULT_SOA_DIR, instance_type: str = None
) -> Iterator[Tuple[str, str]]:
    if service is None:
        service = "*"
    service_path = os.path.join(soa_dir, service)

    valid_clusters = "|".join(load_system_paasta_config().get_clusters())

    if instance_type in INSTANCE_TYPES:
        instance_types = instance_type
    else:
        instance_types = "|".join(INSTANCE_TYPES)

    search_re = r"/.*/(" + instance_types + r")-(" + valid_clusters + r")\.yaml$"

    for yaml_file in glob.glob("%s/*.yaml" % service_path):
        try:
            with open(yaml_file):
                cluster_re_match = re.search(search_re, yaml_file)
                if cluster_re_match is not None:
                    cluster = cluster_re_match.group(2)
                    yield (cluster, yaml_file)
        except IOError as err:
            print(f"Error opening {yaml_file}: {err}")


def list_clusters(
    service: str = None, soa_dir: str = DEFAULT_SOA_DIR, instance_type: str = None
) -> List[str]:
    """Returns a sorted list of clusters a service is configured to deploy to,
    or all clusters if ``service`` is not specified.

    Includes every cluster that has a ``marathon-*.yaml`` or ``tron-*.yaml`` file associated with it.

    :param service: The service name. If unspecified, clusters running any service will be included.
    :returns: A sorted list of cluster names
    """
    clusters = set()
    for cluster, _ in get_soa_cluster_deploy_files(
        service=service, soa_dir=soa_dir, instance_type=instance_type
    ):
        clusters.add(cluster)
    return sorted(clusters)