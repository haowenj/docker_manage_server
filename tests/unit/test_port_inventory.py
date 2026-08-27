from docker_manage_server import runtime_inventory


def test_docker_host_ports_extracts_numeric_mappings_from_all_containers():
    containers = [
        {
            "running": True,
            "ports": {
                "80/tcp": [{"HostPort": "6001"}],
                "53/udp": [{"HostPort": "6010"}],
                "9000/tcp": None,
            },
        },
        {
            "running": False,
            "ports": {"8080/tcp": [{"HostPort": "9999"}]},
        },
        {
            "running": True,
            "ports": {"9000/tcp": [{"HostPort": "not-a-port"}]},
        },
    ]

    assert runtime_inventory.docker_host_ports(containers) == {6001, 6010, 9999}


def test_build_port_overview_marks_mapped_ports_and_returns_available_range():
    overview = runtime_inventory.build_port_overview(
        [
            {"ports": {"80/tcp": [{"HostPort": "6000"}]}},
            {"ports": {"81/tcp": [{"HostPort": "9999"}]}},
        ]
    )

    assert (overview.start, overview.end) == (
        runtime_inventory.PORT_RANGE_START,
        runtime_inventory.PORT_RANGE_END,
    )
    assert overview.total_count == 4000
    assert overview.mapped_ports == (6000, 9999)
    assert overview.available_ports[:2] == (6001, 6002)
    assert overview.available_ports[-1] == 9998
    assert overview.ports[0].mapped is True
    assert overview.ports[1].available is True
