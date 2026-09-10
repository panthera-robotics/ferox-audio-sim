"""Exercise the adapter's actual spin/finally block, no ROS or hardware."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


class ExternalShutdownException(Exception):
    pass


@pytest.mark.parametrize("error", [ExternalShutdownException(), KeyboardInterrupt(),
                                  RuntimeError("fault")])
def test_shutdown_closes_codec_but_does_not_hide_faults(error):
    source = Path(__file__).parents[1] / "ferox_audio_go2/bridge_node.py"
    main = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    block = next(n for n in main.body if isinstance(n, ast.Try)
                 and any(isinstance(x, ast.Expr) and isinstance(x.value, ast.Call)
                         and ast.unparse(x.value.func) == "rclpy.spin" for x in n.body))
    core, node = Mock(), Mock()
    ros = SimpleNamespace(spin=Mock(side_effect=error), ok=lambda: False,
                          shutdown=Mock())
    scope = dict(rclpy=ros, mic_core=core, node=node,
                 ExternalShutdownException=ExternalShutdownException)
    code = compile(ast.Module(body=[block], type_ignores=[]), str(source), "exec")
    if isinstance(error, RuntimeError):
        with pytest.raises(RuntimeError, match="fault"):
            exec(code, scope)
    else:
        exec(code, scope)
    core.close.assert_called_once()
    node.destroy_node.assert_called_once()
    ros.shutdown.assert_not_called()
