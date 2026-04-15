from unittest.mock import Mock

import docker

from tddbench.harness import docker_build


class _DummyTestSpec:
    repo = "dummy_repo"
    version = "dummy_version"
    instance_id = "dummy_instance"
    instance_image_key = "dummy_image:latest"
    platform = "linux/x86_64"

    @staticmethod
    def get_instance_container_name(run_id=None):
        if not run_id:
            return "sweb.eval.dummy_instance"
        return f"sweb.eval.dummy_instance.{run_id}"


def test_build_container_removes_stale_named_container(monkeypatch):
    monkeypatch.setattr(
        docker_build,
        "MAP_REPO_VERSION_TO_SPECS",
        {"dummy_repo": {"dummy_version": {}}},
    )
    monkeypatch.setattr(docker_build, "build_instance_image", lambda *args, **kwargs: None)

    stale_container = Mock()
    stale_container.name = "sweb.eval.dummy_instance.generate-basic"
    stale_container.id = "stale-id"
    new_container = Mock()
    new_container.id = "new-id"

    client = Mock()
    client.containers.get.return_value = stale_container
    client.containers.create.return_value = new_container

    cleanup_mock = Mock()
    monkeypatch.setattr(docker_build, "cleanup_container", cleanup_mock)

    logger = Mock()
    result = docker_build.build_container(
        _DummyTestSpec(), client, "generate-basic", logger, nocache=False
    )

    assert result is new_container
    client.containers.get.assert_called_once_with("sweb.eval.dummy_instance.generate-basic")
    cleanup_mock.assert_called_once_with(client, stale_container, logger)
    client.containers.create.assert_called_once()


def test_build_container_skips_cleanup_when_no_stale_container(monkeypatch):
    monkeypatch.setattr(
        docker_build,
        "MAP_REPO_VERSION_TO_SPECS",
        {"dummy_repo": {"dummy_version": {}}},
    )
    monkeypatch.setattr(docker_build, "build_instance_image", lambda *args, **kwargs: None)

    new_container = Mock()
    new_container.id = "new-id"

    client = Mock()
    client.containers.get.side_effect = docker.errors.NotFound("not found")
    client.containers.create.return_value = new_container

    cleanup_mock = Mock()
    monkeypatch.setattr(docker_build, "cleanup_container", cleanup_mock)

    logger = Mock()
    result = docker_build.build_container(
        _DummyTestSpec(), client, "generate-basic", logger, nocache=False
    )

    assert result is new_container
    cleanup_mock.assert_not_called()
    client.containers.create.assert_called_once()
