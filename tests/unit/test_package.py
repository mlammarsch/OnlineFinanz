"""
Test für das finue-Paket.

N-OPS-01: Paket muss importierbar sein und __version__ bereitstellen.
"""


def test_finue_import() -> None:
    """Teste, dass finue importierbar ist und __version__ hat."""
    import finue

    assert hasattr(finue, "__version__")
    assert isinstance(finue.__version__, str)
    assert finue.__version__ == "0.1.0"
