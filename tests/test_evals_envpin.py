"""Envpin depth (D24): store image digests and a host descriptor
belong in every run row, alongside model/thinking/git-ref."""

from resgraph.evals.runner import _host, _store_images


def test_store_images_cover_stores_and_carry_digests():
    images = _store_images()
    assert {"redis", "memgraph", "postgres"} <= set(images)
    for ref in images.values():
        assert "@sha256:" in ref


def test_host_descriptor_shape():
    host = _host()
    assert host["class"] in {"ci", "laptop"}
    assert host["platform"]
    assert host["machine"]
    assert isinstance(host["cpus"], int) and host["cpus"] >= 1
