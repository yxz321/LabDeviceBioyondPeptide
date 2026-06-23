"""Bioyond peptide station device-operation proxy classes."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import requests

try:
    from unilabos.registry.decorators import action, device
except Exception:  # pragma: no cover - optional registry deps may be absent in offline tests
    def action(*_args: Any, **_kwargs: Any):
        def decorator(func):
            return func
        return decorator

    def device(*_args: Any, **_kwargs: Any):
        def decorator(cls):
            return cls
        return decorator

from unilabos.utils.log import logger

LCMSPlatePosition = Literal["一号位", "二号位"]
NitrogenChannel = Literal["一号通道", "二号通道", "全部通道"]
CentrifugePosition = Literal["Pos1", "Pos2", "Pos3", "Pos4"]
CentrifugePlateType = Literal["_96深孔板", "_384孔板"]
IDOTPlateType = Literal["SourcePlate", "TargetPlate"]

DEFAULT_OPERATION_SNAPSHOT_PATH = "temp_benyao/peptide/_input/api_lims_device_list_operations_2026-06-05_172_20_23_145_44388.json"
DEFAULT_CONFIG_PATH = "temp_benyao/peptide/peptide_station_config.json"


class BioyondDeviceProxyBase:
    """Base class for Bioyond physical-device proxy nodes."""

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        operation_snapshot_path: str = DEFAULT_OPERATION_SNAPSHOT_PATH,
        bioyond_device_name: str = "",
        bioyond_device_id: str = "",
        bioyond_frame_code: Optional[int] = None,
        station_id: str = "bioyond_peptide_station",
        **kwargs: Any,
    ) -> None:
        del kwargs
        self.config_path = str(config_path)
        self.operation_snapshot_path = str(operation_snapshot_path)
        self.bioyond_device_name = str(bioyond_device_name or "").strip()
        self.bioyond_device_id = str(bioyond_device_id or "").strip()
        self.bioyond_frame_code = bioyond_frame_code
        self.station_id = station_id
        self.bioyond_config = self._load_json(self.config_path)
        self.api_host = str(self.bioyond_config.get("api_host") or "").rstrip("/")
        self.api_key = str(self.bioyond_config.get("api_key") or "")
        if not self.api_host or not self.api_key:
            raise ValueError("Bioyond device proxy requires api_host/api_key in station config")
        self.operation_snapshot: Optional[Dict[str, Any]] = None
        self._fixture_device: Optional[Dict[str, Any]] = None
        self._ensure_operation_snapshot_file()

    @staticmethod
    def _load_json(path: str) -> Dict[str, Any]:
        with open(path, encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        if not isinstance(data, dict):
            raise ValueError(f"JSON root must be object: {path}")
        return data

    @staticmethod
    def _now_iso8601() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _fetch_operation_snapshot(self) -> Dict[str, Any]:
        endpoint = f"{self.api_host}/api/lims/device/device-list"
        request_body = {"apiKey": self.api_key, "requestTime": self._now_iso8601(), "data": ""}
        try:
            response = requests.post(
                endpoint,
                data=json.dumps(request_body, ensure_ascii=False),
                headers={"Content-Type": "application/json"},
                timeout=int(self.bioyond_config.get("timeout", 120) or 120),
            )
            snapshot = response.json() if response.status_code == 200 else {"code": 0, "message": response.text}
        except Exception as exc:
            raise RuntimeError(f"Bioyond device-list request failed: {exc}") from exc
        if not isinstance(snapshot, dict):
            raise ValueError("Bioyond device-list returned non-dict response")
        if snapshot.get("code") != 1:
            raise ValueError(f"Bioyond device-list failed: {snapshot.get('message', '')}")
        self.operation_snapshot = snapshot
        return snapshot

    def _ensure_operation_snapshot_file(self) -> None:
        if not self.operation_snapshot_path or os.path.exists(self.operation_snapshot_path):
            return
        try:
            snapshot = self._fetch_operation_snapshot()
            parent_dir = os.path.dirname(self.operation_snapshot_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(self.operation_snapshot_path, "w", encoding="utf-8") as file_obj:
                json.dump(snapshot, file_obj, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error(f"Bioyond device operation snapshot generation failed: {exc}")

    def _load_operation_snapshot(self) -> Dict[str, Any]:
        if self.operation_snapshot is not None:
            return self.operation_snapshot
        if self.operation_snapshot_path and os.path.exists(self.operation_snapshot_path):
            self.operation_snapshot = self._load_json(self.operation_snapshot_path)
            return self.operation_snapshot
        return self._fetch_operation_snapshot()

    def _resolve_fixture_device(self) -> Dict[str, Any]:
        if self._fixture_device is not None:
            return self._fixture_device
        snapshot = self._load_operation_snapshot()
        devices = snapshot.get("data") or []
        for device_item in devices:
            if not isinstance(device_item, dict):
                continue
            if self.bioyond_device_id and str(device_item.get("id") or "") == self.bioyond_device_id:
                self._fixture_device = device_item
                return device_item
            if self.bioyond_device_name and str(device_item.get("deviceName") or "") == self.bioyond_device_name:
                self._fixture_device = device_item
                return device_item
        raise ValueError(f"Bioyond operation fixture lacks device: {self.bioyond_device_name or self.bioyond_device_id}")

    def _resolve_operation_template(self, description: str) -> Dict[str, Any]:
        fixture_device = self._resolve_fixture_device()
        for operation in fixture_device.get("operations") or []:
            if isinstance(operation, dict) and operation.get("description") == description:
                return copy.deepcopy(operation)
        raise ValueError(f"Bioyond fixture device {self.bioyond_device_name} lacks operation: {description}")

    def _coerce_parameter_value(self, parameter: Dict[str, Any], value: Any) -> Any:
        enum_keys = parameter.get("enumKeys") or []
        enum_values = parameter.get("enumValues") or []
        if enum_keys and isinstance(value, str):
            if value not in enum_keys:
                raise ValueError(f"{parameter.get('name')} must be one of {enum_keys}")
            return enum_values[enum_keys.index(value)]
        if parameter.get("isNeedRange"):
            numeric_value = float(value)
            min_value = float(parameter.get("minValue"))
            max_value = float(parameter.get("maxValue"))
            if numeric_value < min_value or numeric_value > max_value:
                raise ValueError(f"{parameter.get('name')} must be in range {min_value}..{max_value}")
        return value

    def _build_operation_payload(self, description: str, parameter_values: Dict[str, Any]) -> Dict[str, Any]:
        operation = self._resolve_operation_template(description)
        for parameter in operation.get("parameters") or []:
            parameter_name = parameter.get("name")
            if parameter_name in parameter_values:
                parameter["value"] = self._coerce_parameter_value(parameter, parameter_values[parameter_name])
        return operation

    def _execute_operation(self, description: str, parameter_values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        endpoint = f"{self.api_host}/api/lims/device/execute-operation"
        operation = self._build_operation_payload(description, parameter_values or {})
        request_body = {"apiKey": self.api_key, "requestTime": self._now_iso8601(), "data": operation}
        try:
            response = requests.post(
                endpoint,
                data=json.dumps(request_body, ensure_ascii=False),
                headers={"Content-Type": "application/json"},
                timeout=int(self.bioyond_config.get("timeout", 120) or 120),
            )
            response_data = response.json() if response.status_code == 200 else {"code": 0, "message": response.text}
        except Exception as exc:
            logger.error(f"Bioyond device operation failed: {exc}")
            response_data = {"code": 0, "message": str(exc)}
        if not isinstance(response_data, dict):
            response_data = {"code": 0, "message": "Bioyond execute-operation returned non-dict response", "data": response_data}
        return {
            "success": response_data.get("code") == 1,
            "code": response_data.get("code"),
            "message": response_data.get("message", ""),
            "timestamp": response_data.get("timestamp"),
            "data": response_data.get("data"),
            "endpoint": endpoint,
            "device_name": self.bioyond_device_name or (self._fixture_device or {}).get("deviceName", ""),
            "operation": description,
            "response": response_data,
            "submitted_operation": operation,
        }


@device(
    id="bioyond_proxy_peptide_robot",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站协作机器人代理设备",
    displayname="协作机器人",
)
class BioyondRobotProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='下使能')
    def disable_robot(self, **kwargs: Any) -> Dict[str, Any]:
        """下使能。"""
        del kwargs
        return self._execute_operation('下使能', {})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='Reset')
    def reset(self, **kwargs: Any) -> Dict[str, Any]:
        """Reset。"""
        del kwargs
        return self._execute_operation('Reset', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_lcms",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站 LCMS 代理设备",
    displayname="LCMS",
)
class BioyondLCMSProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='进板')
    def load_plate(self, plateNo: LCMSPlatePosition = '一号位', **kwargs: Any) -> Dict[str, Any]:
        """进板。"""
        del kwargs
        return self._execute_operation('进板', {"plateNo": plateNo})

    @action(always_free=True, description='出板')
    def unload_plate(self, plateNo: LCMSPlatePosition = '一号位', **kwargs: Any) -> Dict[str, Any]:
        """出板。"""
        del kwargs
        return self._execute_operation('出板', {"plateNo": plateNo})

    @action(always_free=True, description='执行移液程序')
    def run_liquid_transfer_protocol(self, protocol: str = "", timeout: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """执行移液程序。"""
        del kwargs
        return self._execute_operation('执行移液程序', {"protocol": protocol, "timeout": timeout})

    @action(always_free=True, description='重置')
    def reset_state(self, **kwargs: Any) -> Dict[str, Any]:
        """重置。"""
        del kwargs
        return self._execute_operation('重置', {})

    @action(always_free=True, description='初始化')
    def initialize(self, **kwargs: Any) -> Dict[str, Any]:
        """初始化。"""
        del kwargs
        return self._execute_operation('初始化', {})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})


@device(
    id="bioyond_proxy_peptide_tecan_liquid_handler",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站自动移液工作站代理设备",
    displayname="自动移液工作站",
)
class BioyondTecanLiquidHandlerProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='转板1')
    def transfer_plate_1(self, protocol: str = "", sourcePosition: int = 0, targetPosition: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """转板1。"""
        del kwargs
        return self._execute_operation('转板1', {"protocol": protocol, "sourcePosition": sourcePosition, "targetPosition": targetPosition})

    @action(always_free=True, description='移液')
    def liquid_transfer(self, protocol: str = "", parameters: str = "", **kwargs: Any) -> Dict[str, Any]:
        """移液。"""
        del kwargs
        return self._execute_operation('移液', {"protocol": protocol, "parameters": parameters})

    @action(always_free=True, description='板位倾斜')
    def tilt_plate_position(self, angle: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """板位倾斜。"""
        del kwargs
        return self._execute_operation('板位倾斜', {"angle": angle})

    @action(always_free=True, description='启动震荡')
    def start_shaking(self, deviceNo: int = 0, speed: int = 0, temperature: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """启动震荡。"""
        del kwargs
        return self._execute_operation('启动震荡', {"deviceNo": deviceNo, "speed": speed, "temperature": temperature})

    @action(always_free=True, description='停止震荡')
    def stop_shaking(self, deviceNo: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """停止震荡。"""
        del kwargs
        return self._execute_operation('停止震荡', {"deviceNo": deviceNo})

    @action(always_free=True, description='初始化')
    def initialize(self, **kwargs: Any) -> Dict[str, Any]:
        """初始化。"""
        del kwargs
        return self._execute_operation('初始化', {})

    @action(always_free=True, description='移液2')
    def liquid_transfer_2(self, protocol: str = "", volume: float = 0.0, position: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """移液2。"""
        del kwargs
        return self._execute_operation('移液2', {"protocol": protocol, "volume": volume, "position": position})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='复位')
    def reset_device(self, **kwargs: Any) -> Dict[str, Any]:
        """复位。"""
        del kwargs
        return self._execute_operation('复位', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_g3_liquid_handler",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站液体工作站代理设备",
    displayname="液体工作站",
)
class BioyondG3LiquidHandlerProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='设置灯光状态')
    def set_light_status(self, lightStatus: bool = False, **kwargs: Any) -> Dict[str, Any]:
        """设置灯光状态。"""
        del kwargs
        return self._execute_operation('设置灯光状态', {"lightStatus": lightStatus})

    @action(always_free=True, description='重置布局')
    def reset_layout(self, **kwargs: Any) -> Dict[str, Any]:
        """重置布局。"""
        del kwargs
        return self._execute_operation('重置布局', {})

    @action(always_free=True, description='紫外灯控制')
    def set_uv_light(self, isOpen: bool = False, **kwargs: Any) -> Dict[str, Any]:
        """紫外灯控制。"""
        del kwargs
        return self._execute_operation('紫外灯控制', {"isOpen": isOpen})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='Reset')
    def reset(self, **kwargs: Any) -> Dict[str, Any]:
        """Reset。"""
        del kwargs
        return self._execute_operation('Reset', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})

    @action(always_free=True, description='正压')
    def positive_pressure(self, **kwargs: Any) -> Dict[str, Any]:
        """正压。"""
        del kwargs
        return self._execute_operation('正压', {})

    @action(always_free=True, description='负压')
    def negative_pressure(self, workTime: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """负压。"""
        del kwargs
        return self._execute_operation('负压', {"workTime": workTime})

    @action(always_free=True, description='气缸夹紧')
    def clamp_cylinder(self, **kwargs: Any) -> Dict[str, Any]:
        """气缸夹紧。"""
        del kwargs
        return self._execute_operation('气缸夹紧', {})

    @action(always_free=True, description='气缸松开')
    def release_cylinder(self, **kwargs: Any) -> Dict[str, Any]:
        """气缸松开。"""
        del kwargs
        return self._execute_operation('气缸松开', {})

    @action(always_free=True, description='停止正负压')
    def stop_pressure(self, **kwargs: Any) -> Dict[str, Any]:
        """停止正负压。"""
        del kwargs
        return self._execute_operation('停止正负压', {})


@device(
    id="bioyond_proxy_peptide_idot_liquid_handler",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站 IDOT 代理设备",
    displayname="非接触式纳升级移液系统",
)
class BioyondIDOTLiquidHandlerProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='执行移液程序')
    def run_liquid_transfer_protocol(self, protocol: str = "", timeout: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """执行移液程序。"""
        del kwargs
        return self._execute_operation('执行移液程序', {"protocol": protocol, "timeout": timeout})

    @action(always_free=True, description='出板')
    def unload_plate(self, plateType: IDOTPlateType = 'SourcePlate', **kwargs: Any) -> Dict[str, Any]:
        """出板。"""
        del kwargs
        return self._execute_operation('出板', {"plateType": plateType})

    @action(always_free=True, description='进板')
    def load_plate(self, **kwargs: Any) -> Dict[str, Any]:
        """进板。"""
        del kwargs
        return self._execute_operation('进板', {})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})


@device(
    id="bioyond_proxy_peptide_synthesizer",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站全自动多肽合成系统代理设备",
    displayname="全自动多肽合成系统",
)
class BioyondPeptideSynthesizerProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='运行协议')
    def run_protocol(self, sequenceFileName: str = "", methodFileName: str = "", timeout: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """运行协议。"""
        del kwargs
        return self._execute_operation('运行协议', {"sequenceFileName": sequenceFileName, "methodFileName": methodFileName, "timeout": timeout})

    @action(always_free=True, description='准备运行数据并获取报表')
    def prepare_run_data_and_get_report(self, derivatives: str = "", sequence: str = "", methodFileName: str = "", **kwargs: Any) -> Dict[str, Any]:
        """准备运行数据并获取报表。"""
        del kwargs
        return self._execute_operation('准备运行数据并获取报表', {"derivatives": derivatives, "sequence": sequence, "methodFileName": methodFileName})

    @action(always_free=True, description='开始运行')
    def start_run(self, timeout: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """开始运行。"""
        del kwargs
        return self._execute_operation('开始运行', {"timeout": timeout})

    @action(always_free=True, description='获取报表')
    def get_report(self, **kwargs: Any) -> Dict[str, Any]:
        """获取报表。"""
        del kwargs
        return self._execute_operation('获取报表', {})

    @action(always_free=True, description='开始运行(集成气缸)')
    def start_run_with_cylinder(self, timeout: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """开始运行(集成气缸)。"""
        del kwargs
        return self._execute_operation('开始运行(集成气缸)', {"timeout": timeout})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='复位')
    def reset_device(self, **kwargs: Any) -> Dict[str, Any]:
        """复位。"""
        del kwargs
        return self._execute_operation('复位', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})

    @action(always_free=True, description='CEM气缸夹紧')
    def clamp_cem_cylinder(self, **kwargs: Any) -> Dict[str, Any]:
        """CEM气缸夹紧。"""
        del kwargs
        return self._execute_operation('CEM气缸夹紧', {})

    @action(always_free=True, description='CEM气缸松开')
    def release_cem_cylinder(self, **kwargs: Any) -> Dict[str, Any]:
        """CEM气缸松开。"""
        del kwargs
        return self._execute_operation('CEM气缸松开', {})


@device(
    id="bioyond_proxy_peptide_plate_sealer",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站全自动热封膜仪代理设备",
    displayname="全自动热封膜仪",
)
class BioyondPlateSealerProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='开始封膜')
    def start_sealing(self, plateType: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """开始封膜。"""
        del kwargs
        return self._execute_operation('开始封膜', {"plateType": plateType})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})


@device(
    id="bioyond_proxy_peptide_nitrogen_blow",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站双位氮吹仪代理设备",
    displayname="双位氮吹仪",
)
class BioyondNitrogenBlowProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='启动氮吹')
    def start_nitrogen_blow(self, channelNo: NitrogenChannel = '一号通道', temperature: int = 0, airPressure: int = 0, workTime: int = 0, temperature2: int = 0, airPressure2: int = 0, workTime2: int = 0, height: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """启动氮吹。"""
        del kwargs
        return self._execute_operation('启动氮吹', {"channelNo": channelNo, "temperature": temperature, "airPressure": airPressure, "workTime": workTime, "temperature2": temperature2, "airPressure2": airPressure2, "workTime2": workTime2, "height": height})

    @action(always_free=True, description='停止氮吹')
    def stop_nitrogen_blow(self, channelNo: NitrogenChannel = '一号通道', **kwargs: Any) -> Dict[str, Any]:
        """停止氮吹。"""
        del kwargs
        return self._execute_operation('停止氮吹', {"channelNo": channelNo})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='复位')
    def reset_device(self, **kwargs: Any) -> Dict[str, Any]:
        """复位。"""
        del kwargs
        return self._execute_operation('复位', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_centrifuge",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站定位离心机代理设备",
    displayname="定位离心机",
)
class BioyondCentrifugeProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='开始工作')
    def start_work(self, temperature: float = 0.0, speed: int = 0, workTime: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """开始工作。"""
        del kwargs
        return self._execute_operation('开始工作', {"temperature": temperature, "speed": speed, "workTime": workTime})

    @action(always_free=True, description='开门')
    def open_door(self, **kwargs: Any) -> Dict[str, Any]:
        """开门。"""
        del kwargs
        return self._execute_operation('开门', {})

    @action(always_free=True, description='关门')
    def close_door(self, **kwargs: Any) -> Dict[str, Any]:
        """关门。"""
        del kwargs
        return self._execute_operation('关门', {})

    @action(always_free=True, description='停止工作')
    def stop_work(self, **kwargs: Any) -> Dict[str, Any]:
        """停止工作。"""
        del kwargs
        return self._execute_operation('停止工作', {})

    @action(always_free=True, description='旋转吊篮')
    def rotate_bucket(self, pos: CentrifugePosition = 'Pos1', **kwargs: Any) -> Dict[str, Any]:
        """旋转吊篮。"""
        del kwargs
        return self._execute_operation('旋转吊篮', {"pos": pos})

    @action(always_free=True, description='进板')
    def load_plate(self, plateNo: CentrifugePosition = 'Pos1', plateType: CentrifugePlateType = '_96深孔板', **kwargs: Any) -> Dict[str, Any]:
        """进板。"""
        del kwargs
        return self._execute_operation('进板', {"plateNo": plateNo, "plateType": plateType})

    @action(always_free=True, description='出板')
    def unload_plate(self, plateNo: CentrifugePosition = 'Pos1', plateType: CentrifugePlateType = '_96深孔板', **kwargs: Any) -> Dict[str, Any]:
        """出板。"""
        del kwargs
        return self._execute_operation('出板', {"plateNo": plateNo, "plateType": plateType})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='Reset')
    def reset(self, **kwargs: Any) -> Dict[str, Any]:
        """Reset。"""
        del kwargs
        return self._execute_operation('Reset', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_plate_peeler",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站全自动撕膜仪代理设备",
    displayname="全自动撕膜仪",
)
class BioyondPlatePeelerProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})


@device(
    id="bioyond_proxy_peptide_fridge",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站冰箱代理设备",
    displayname="冰箱",
)
class BioyondFridgeProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='开门')
    def open_door(self, **kwargs: Any) -> Dict[str, Any]:
        """开门。"""
        del kwargs
        return self._execute_operation('开门', {})

    @action(always_free=True, description='关门')
    def close_door(self, **kwargs: Any) -> Dict[str, Any]:
        """关门。"""
        del kwargs
        return self._execute_operation('关门', {})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='复位')
    def reset_device(self, **kwargs: Any) -> Dict[str, Any]:
        """复位。"""
        del kwargs
        return self._execute_operation('复位', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_hotel",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站自动化堆栈代理设备",
    displayname="自动化堆栈",
)
class BioyondHotelProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='堆栈出库')
    def hotel_out(self, posX: int = 0, posY: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """堆栈出库。"""
        del kwargs
        return self._execute_operation('堆栈出库', {"posX": posX, "posY": posY})

    @action(always_free=True, description='堆栈入库')
    def hotel_in(self, posX: int = 0, posY: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """堆栈入库。"""
        del kwargs
        return self._execute_operation('堆栈入库', {"posX": posX, "posY": posY})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})


@device(
    id="bioyond_proxy_peptide_default_stack",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站缓冲位代理设备",
    displayname="缓冲位",
)
class BioyondDefaultStackProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})


@device(
    id="bioyond_proxy_peptide_turntable",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站转台代理设备",
    displayname="转台",
)
class BioyondTurntableProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='旋转到绝对角度')
    def rotate_to_absolute_angle(self, angle: float = 0.0, **kwargs: Any) -> Dict[str, Any]:
        """旋转到绝对角度。"""
        del kwargs
        return self._execute_operation('旋转到绝对角度', {"angle": angle})

    @action(always_free=True, description='旋转到指定位置')
    def rotate_to_position(self, angleEnum: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """旋转到指定位置。"""
        del kwargs
        return self._execute_operation('旋转到指定位置', {"angleEnum": angleEnum})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='Reset')
    def reset(self, **kwargs: Any) -> Dict[str, Any]:
        """Reset。"""
        del kwargs
        return self._execute_operation('Reset', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_hotel_inout_pos",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站自动堆栈上下料位代理设备",
    displayname="自动堆栈上下料位",
)
class BioyondHotelInOutPosProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='复位')
    def reset_device(self, **kwargs: Any) -> Dict[str, Any]:
        """复位。"""
        del kwargs
        return self._execute_operation('复位', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_plate_reader",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站酶标仪代理设备",
    displayname="酶标仪",
)
class BioyondPlateReaderProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='进板')
    def load_plate(self, **kwargs: Any) -> Dict[str, Any]:
        """进板。"""
        del kwargs
        return self._execute_operation('进板', {})

    @action(always_free=True, description='出板')
    def unload_plate(self, **kwargs: Any) -> Dict[str, Any]:
        """出板。"""
        del kwargs
        return self._execute_operation('出板', {})

    @action(always_free=True, description='运行协议')
    def run_protocol(self, protocol: str = "", timeout: int = 0, **kwargs: Any) -> Dict[str, Any]:
        """运行协议。"""
        del kwargs
        return self._execute_operation('运行协议', {"protocol": protocol, "timeout": timeout})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})


@device(
    id="bioyond_proxy_peptide_label_printer",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站贴标机代理设备",
    displayname="贴标机",
)
class BioyondLabelPrinterProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='BY_Print')
    def print_label(self, code: str = "", **kwargs: Any) -> Dict[str, Any]:
        """BY_Print。"""
        del kwargs
        return self._execute_operation('BY_Print', {"code": code})

    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})

    @action(always_free=True, description='复位')
    def reset_device(self, **kwargs: Any) -> Dict[str, Any]:
        """复位。"""
        del kwargs
        return self._execute_operation('复位', {})

    @action(always_free=True, description='重连')
    def reconnect(self, **kwargs: Any) -> Dict[str, Any]:
        """重连。"""
        del kwargs
        return self._execute_operation('重连', {})


@device(
    id="bioyond_proxy_peptide_safety_monitor",
    category=["device", "bioyond", "peptide"],
    description="Bioyond 多肽站安全门监控代理设备",
    displayname="安全门监控",
)
class BioyondSafetyMonitorProxy(BioyondDeviceProxyBase):
    @action(always_free=True, description='清错')
    def clear_error(self, **kwargs: Any) -> Dict[str, Any]:
        """清错。"""
        del kwargs
        return self._execute_operation('清错', {})
