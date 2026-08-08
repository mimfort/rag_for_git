import pytest

from reviewer.config.provider_access import ProviderAccessSpec, render_provider_access


def test_render_provider_access_has_stable_complete_order():
    access = ProviderAccessSpec(
        minimum_permissions="Issues: Read and write",
        read_operations=("читать задачи", "читать комментарии"),
        write_operations=("создавать задачи", "закрывать задачи"),
        validation="identity и доступ к проекту",
    )

    text = render_provider_access(
        label="Example",
        help_text="Создайте token.",
        help_url="https://example.test/token",
        access=access,
    )

    markers = (
        "Example",
        "Создайте token.",
        "Минимальные права: Issues: Read and write",
        "Чтение: читать задачи; читать комментарии",
        "Запись: создавать задачи; закрывать задачи",
        "Проверка: identity и доступ к проекту",
        "https://example.test/token",
    )
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "field",
    ("minimum_permissions", "read_operations", "write_operations", "validation"),
)
def test_provider_access_rejects_empty_contract(field):
    values = {
        "minimum_permissions": "read/write",
        "read_operations": ("read",),
        "write_operations": ("write",),
        "validation": "identity",
    }
    values[field] = () if field.endswith("operations") else ""

    with pytest.raises(ValueError, match=field):
        ProviderAccessSpec(**values)
