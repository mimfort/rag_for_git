from reviewer.config.provider_access import ProviderAccessSpec


FAKE_PROVIDER_ACCESS = ProviderAccessSpec(
    minimum_permissions="test read/write",
    read_operations=("read test data",),
    write_operations=("write test data",),
    validation="test identity",
)
