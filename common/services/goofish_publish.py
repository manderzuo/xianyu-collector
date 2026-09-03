"""闲鱼发布能力检测与网页接口发布器。

这里保留平台动作的真实边界：能力检测只调用发布初始化接口；正式发布
才会上传媒体并调用发布接口，任何 Cookie 失效、风控和业务失败都会原样
转换为失败结果，不写入伪成功记录。
"""
from __future__ import annotations

import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from common.config import settings
from common.services.amap_inputtips import AmapInputTipsError, search_input_tips
from common.services.goofish_mtop import mtop_call


PREGET_API = "mtop.idle.pc.idleitem.preget"
PERSONAL_PUBLISH_API = "mtop.idle.pc.idleitem.publish"
FISH_SHOP_PUBLISH_API = "mtop.idle.pc.backend.idleitem.publish"
SERVICE_CARDS_API = "mtop.idle.item.publish.service.cards.list"
DRAFT_PUBLISH_API = "mtop.idle.idleitem.draft.publish"
IMAGE_UPLOAD_URL = "https://stream-upload.goofish.com/api/upload.api?floderId=0&appkey=fleamarket&_input_charset=utf-8"


class GoofishPublishError(RuntimeError):
    def __init__(self, message: str, *, account_invalid: bool = False) -> None:
        super().__init__(message)
        self.account_invalid = account_invalid


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _price_cent(value: Any, label: str) -> str:
    try:
        cents = round(float(value) * 100)
    except (TypeError, ValueError) as exc:
        raise GoofishPublishError(f"{label}格式不正确") from exc
    if cents <= 0:
        raise GoofishPublishError(f"{label}必须大于0")
    return str(cents)


def _quantity(value: Any) -> int:
    """校验单品库存，普通卖家商品也要把该数量提交给平台。"""
    try:
        quantity = int(value or 1)
    except (TypeError, ValueError) as exc:
        raise GoofishPublishError("线上库存必须是整数") from exc
    if quantity < 1 or quantity > 999999:
        raise GoofishPublishError("线上库存必须在1到999999之间")
    return quantity


def _content_type(name: str) -> str:
    value = mimetypes.guess_type(name)[0] or "image/jpeg"
    return value if value.startswith("image/") else "image/jpeg"


def _image_dimensions(content: bytes, name: str) -> tuple[int, int]:
    """不依赖图像库读取常见图片尺寸；异常时给平台一个安全默认值。"""
    try:
        if content.startswith(b"\x89PNG") and len(content) >= 24:
            return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
        if content.startswith((b"GIF87a", b"GIF89a")) and len(content) >= 10:
            return int.from_bytes(content[6:8], "little"), int.from_bytes(content[8:10], "little")
        if content.startswith(b"\xff\xd8"):
            pos = 2
            while pos + 9 < len(content):
                if content[pos] != 0xFF:
                    pos += 1
                    continue
                marker = content[pos + 1]
                pos += 2
                if marker in {0xD8, 0xD9}:
                    continue
                length = int.from_bytes(content[pos:pos + 2], "big")
                if marker in range(0xC0, 0xC4) or marker in range(0xC5, 0xC8) or marker in range(0xC9, 0xCC) or marker in range(0xCD, 0xD0):
                    return int.from_bytes(content[pos + 5:pos + 7], "big"), int.from_bytes(content[pos + 3:pos + 5], "big")
                pos += max(length, 2)
    except (IndexError, ValueError):
        pass
    return 800, 800


def _resolve_file(value: str) -> tuple[bytes, str, str] | None:
    normalized = value.strip()
    if not normalized or normalized.startswith(("http://", "https://")):
        return None
    path = Path(normalized)
    if normalized.startswith("/static/") or normalized.startswith("static/"):
        path = Path(settings.static_dir) / normalized.lstrip("/").removeprefix("static/")
    if not path.is_file():
        raise GoofishPublishError(f"图片文件不存在：{path}")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise GoofishPublishError(f"读取图片失败：{path}") from exc
    if not content:
        raise GoofishPublishError(f"图片文件为空：{path}")
    return content, path.name, _content_type(path.name)


async def _read_image(value: str) -> tuple[bytes, str, str]:
    local = _resolve_file(value)
    if local:
        return local
    normalized = value.strip()
    if not normalized.startswith(("http://", "https://")):
        raise GoofishPublishError("图片地址格式不正确")
    try:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            response = await client.get(normalized)
            response.raise_for_status()
            content = response.content
    except httpx.HTTPError as exc:
        raise GoofishPublishError(f"远程图片下载失败：{exc}") from exc
    if not content:
        raise GoofishPublishError("远程图片内容为空")
    name = Path(urlparse(normalized).path).name or "publish-image.jpg"
    return content, name, _content_type(name)


async def _upload_image(value: str, cookie: str) -> dict[str, Any]:
    content, name, content_type = await _read_image(value)
    width, height = _image_dimensions(content, name)
    # 闲鱼 stream-upload 会校验 multipart 文件名长度。前端本地上传的
    # 文件名通常已经包含 UUID，若再把它和时间戳拼接，会触发
    # FILE_NAME_LENGTH_IS_OVER_RANGE。原发布器只使用短随机文件名。
    suffix = Path(name).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        suffix = ".jpg"
    upload_name = f"publish_api_{uuid4().hex}{suffix}"
    try:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            response = await client.post(
                IMAGE_UPLOAD_URL,
                files={"file": (upload_name, content, content_type)},
                headers={
                    "Accept": "*/*",
                    "Cookie": cookie,
                    "Origin": "https://seller.goofish.com",
                    "Referer": "https://seller.goofish.com/?site=COMMONPRO",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise GoofishPublishError(f"闲鱼图片上传失败：{exc}") from exc
    uploaded = body.get("object") if isinstance(body, dict) else None
    if not isinstance(uploaded, dict) or not uploaded.get("url") or body.get("success") is not True:
        platform_message = _text(body.get("message")) if isinstance(body, dict) else ""
        detail = f"：{platform_message}" if platform_message else "：接口未返回有效图片地址"
        raise GoofishPublishError(f"闲鱼图片上传失败{detail}")
    pix = _text(uploaded.get("pix"))
    try:
        pix_width, pix_height = (int(item) for item in pix.lower().split("x", 1))
    except (ValueError, TypeError):
        pix_width, pix_height = width, height
    return {
        "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
        "isQrCode": False,
        "url": str(uploaded["url"]),
        "heightSize": pix_height,
        "widthSize": pix_width,
        "major": False,
        "type": 0,
        "status": "done",
    }


def _post_fee(item_data: dict[str, Any], *, personal: bool) -> dict[str, Any]:
    method = _text(item_data.get("shipping_method")) or "free"
    pickup = bool(item_data.get("support_pickup"))
    if method == "free":
        return {"canFreeShipping": True, "supportFreight": True, "onlyTakeSelf": pickup}
    if personal and method == "distance":
        return {"canFreeShipping": False, "supportFreight": True, "onlyTakeSelf": pickup, "templateId": "-100"}
    if personal and method == "fixed":
        postage = float(item_data.get("postage") or 0)
        if not 0 <= postage <= 1000:
            raise GoofishPublishError("宝贝运费必须在0到1000元之间")
        return {"canFreeShipping": False, "supportFreight": True, "onlyTakeSelf": pickup, "postPriceInCent": str(round(postage * 100)), "templateId": "0"}
    if method == "none":
        return {"canFreeShipping": False, "supportFreight": False, "onlyTakeSelf": pickup, "templateId": "0"}
    raise GoofishPublishError("当前账号不支持运费模板，请选择包邮、按距离计费、一口价或无需邮寄")


def _category_labels(item_data: dict[str, Any]) -> list[dict[str, Any]]:
    channel_id = _text(item_data.get("platform_channel_category_id"))
    channel_name = _text(item_data.get("platform_channel_category_name"))
    category_name = _text(item_data.get("platform_category_name"))
    tb_id = _text(item_data.get("platform_tb_category_id"))
    # 新频道分类接口可能不再返回 tbCatId，但 catId + channelCatId
    # 仍足以组成发布请求。老分类继续沿用 tbCatId；仅当两种分类 ID
    # 都不存在时才阻止发布，避免把合法的新频道分类误判为不完整。
    category_id = _text(item_data.get("platform_category_id")) or tb_id
    if not channel_id or not channel_name or not category_id:
        raise GoofishPublishError("平台商品分类信息不完整，请先重新选择商品分类")
    labels = [{
        "channelCateName": channel_name, "valueId": None, "channelCateId": channel_id,
        "valueName": None, "tbCatId": tb_id or None, "subPropertyId": None, "labelType": "common",
        "subValueId": None, "labelId": None, "propertyName": "分类", "isUserClick": "1",
        "isUserCancel": None, "from": "newPublishChoice", "propertyId": "-10000",
        "labelFrom": "newPublish", "text": category_name or channel_name,
        "properties": f"-10000##分类:{channel_id}##{category_name or channel_name}",
    }]
    for attribute in item_data.get("platform_attributes") or []:
        if not isinstance(attribute, dict):
            continue
        name = _text(attribute.get("property_name"))
        value = _text(attribute.get("value_name") or attribute.get("text"))
        property_id = _text(attribute.get("property_id"))
        if not name or name == "分类" or not value or not property_id:
            continue
        value_id = _text(attribute.get("value_id"))
        labels.append({
            "channelCateName": channel_name, "valueId": value_id, "channelCateId": channel_id,
            "valueName": value, "tbCatId": tb_id or None, "subPropertyId": None, "labelType": "common",
            "subValueId": None, "labelId": None, "propertyName": name, "isUserClick": "1",
            "isUserCancel": None, "from": "newPublishChoice", "propertyId": property_id,
            "labelFrom": "newPublish", "text": value,
            "properties": _text(attribute.get("properties")) or f"{property_id}##{name}:{value_id}##{value}",
        })
    return labels


def _location_to_gps(location: str) -> str:
    """将高德返回的经度,纬度转换为闲鱼接口要求的纬度,经度。"""
    parts = [part.strip() for part in location.split(",")]
    if len(parts) != 2:
        raise GoofishPublishError("宝贝所在地缺少有效坐标，请重新选择地址")
    try:
        longitude = float(parts[0])
        latitude = float(parts[1])
    except (TypeError, ValueError) as exc:
        raise GoofishPublishError("宝贝所在地坐标格式不正确，请重新选择地址") from exc
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise GoofishPublishError("宝贝所在地坐标无效，请重新选择地址")
    return f"{latitude:.6f},{longitude:.6f}"


def _address_match_score(tip: dict[str, Any], expected_text: str) -> tuple[int, int]:
    """优先选中用户在地址弹窗里实际点击的那条高德候选。"""
    if not expected_text:
        return (3, 0)
    expected = "".join(expected_text.split())
    candidate = _text(tip.get("expected_text")) or " / ".join(
        value for value in (
            _text(tip.get("name")),
            _text(tip.get("district")),
            _text(tip.get("address")),
        ) if value
    )
    candidate = "".join(candidate.split())
    if candidate == expected:
        return (0, len(candidate))
    if candidate.startswith(expected):
        return (1, len(candidate))
    if expected in candidate:
        return (2, candidate.find(expected))
    return (4, len(candidate))


async def _resolve_item_address(item_data: dict[str, Any]) -> dict[str, str]:
    """把表单中的地址文字解析为闲鱼要求的 POI、行政区划和 GPS。"""
    address = _text(item_data.get("address"))
    if not address:
        raise GoofishPublishError("请先选择带有效坐标的宝贝所在地")
    expected_text = _text(item_data.get("address_expected_text"))
    try:
        result = await search_input_tips(address)
    except AmapInputTipsError as exc:
        raise GoofishPublishError(str(exc)) from exc

    tips = result.get("tips") if isinstance(result, dict) else []
    valid_tips = [
        tip for tip in tips
        if isinstance(tip, dict)
        and _text(tip.get("id"))
        and _text(tip.get("adcode"))
        and _text(tip.get("location"))
    ]
    if not valid_tips:
        raise GoofishPublishError("宝贝所在地未返回 POI 编号、行政区划或坐标，请重新选择地址")
    selected = min(valid_tips, key=lambda tip: _address_match_score(tip, expected_text))
    return {
        "divisionId": _text(selected.get("adcode")),
        "gps": _location_to_gps(_text(selected.get("location"))),
        "poiId": _text(selected.get("id")),
        "poiName": _text(selected.get("name")),
    }


def _sku_payload(item_data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    specifications = item_data.get("specifications") or []
    rows = item_data.get("sku_rows") or []
    if not specifications and not rows:
        return [], [], False
    if not specifications or not rows:
        raise GoofishPublishError("商品规格数据不完整，请完善规格组合后再发布")
    properties: list[dict[str, Any]] = []
    names: list[str] = []
    allowed: dict[str, set[str]] = {}
    expected = 1
    for spec in specifications:
        name = _text(spec.get("name")) if isinstance(spec, dict) else ""
        values = [_text(value.get("name") if isinstance(value, dict) else value) for value in (spec.get("values") or [])] if isinstance(spec, dict) else []
        values = [value for value in values if value]
        if not name or not values or name in allowed or len(values) != len(set(values)):
            raise GoofishPublishError("商品规格名称或规格值不完整/重复")
        names.append(name); allowed[name] = set(values); expected *= len(values)
        properties.append({"propertyName": name, "supportImage": bool(spec.get("support_image")), "propertyValues": [{"propertyValue": value} for value in values]})
    if len(rows) != expected:
        raise GoofishPublishError(f"规格组合数量不完整：应有 {expected} 组，实际收到 {len(rows)} 组")
    sku_rows = []
    combinations: set[tuple[str, ...]] = set()
    for row in rows:
        specs = row.get("specs") if isinstance(row, dict) else {}
        combination = tuple(_text(specs.get(name)) for name in names)
        if any(value not in allowed[name] for name, value in zip(names, combination)) or combination in combinations:
            raise GoofishPublishError("SKU 规格组合无效或重复")
        combinations.add(combination)
        stock = int(row.get("stock", 0))
        if stock < 0:
            raise GoofishPublishError("SKU 库存不能为负数")
        sku_rows.append({"priceInCent": _price_cent(row.get("price"), "SKU售价"), "quantity": stock, "propertyList": [{"propertyText": name, "valueText": value} for name, value in zip(names, combination)]})
    return properties, sku_rows, True


def _item_reference(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        found_id = None
        found_url = None
        for key, item in value.items():
            lower = key.lower()
            if lower in {"itemid", "item_id", "idleitemid"} and item is not None:
                found_id = _text(item) or found_id
            elif lower in {"itemurl", "item_url", "url"} and isinstance(item, str) and "goofish.com" in item:
                found_url = item
                match = re.search(r"(?:[?&]id=|/item/)(\d+)", item)
                found_id = found_id or (match.group(1) if match else None)
            child_id, child_url = _item_reference(item)
            found_id = found_id or child_id; found_url = found_url or child_url
        return found_id, found_url
    if isinstance(value, list):
        for item in value:
            found_id, found_url = _item_reference(item)
            if found_id or found_url:
                return found_id, found_url
    return None, None


def _draft_reference(value: Any) -> str | None:
    """从草稿接口的嵌套响应中提取 draftId，不把普通 itemId 混用。"""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"draftid", "draft_id"} and item is not None:
                found = _text(item)
                if found:
                    return found
            found = _draft_reference(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _draft_reference(item)
            if found:
                return found
    return None


def _is_service_category(item_data: dict[str, Any]) -> bool:
    if bool(item_data.get("is_service_category")):
        return True
    path = item_data.get("platform_category_path") or []
    if not isinstance(path, list):
        return False
    return any(
        isinstance(item, dict)
        and (_text(item.get("id")) == "201450801" or _text(item.get("name")) == "服务")
        for item in path
    )


def _draft_payload(values: dict[str, Any]) -> dict[str, Any]:
    """生成与闲鱼网页 ``t7.xP`` 等价的服务类草稿数据。

    服务分类的网页流程不是把普通发布请求改名后提交，而是先将表单
    标准化成 draft.publish 所需的完整结构。缺少这些默认字段时，接口
    仍可能返回 draftId，但 APP 会按普通 ``发闲置`` 草稿打开。
    """
    address = values.get("itemAddrDTO") if isinstance(values.get("itemAddrDTO"), dict) else {}
    post_fee = values.get("itemPostFeeDTO") if isinstance(values.get("itemPostFeeDTO"), dict) else {}
    price = values.get("itemPriceDTO") if isinstance(values.get("itemPriceDTO"), dict) else {}
    text = values.get("itemTextDTO") if isinstance(values.get("itemTextDTO"), dict) else {}
    labels = values.get("itemLabelExtList") if isinstance(values.get("itemLabelExtList"), list) else []
    protocols = values.get("userRightsProtocols") if isinstance(values.get("userRightsProtocols"), list) else []
    images = values.get("imageInfoDOList") if isinstance(values.get("imageInfoDOList"), list) else []
    item_properties = values.get("itemProperties") if isinstance(values.get("itemProperties"), list) else []
    item_skus = values.get("itemSkuList") if isinstance(values.get("itemSkuList"), list) else []

    normalized_images = []
    for image in images:
        if not isinstance(image, dict):
            continue
        normalized_images.append({
            "extraInfo": image.get("extraInfo") if isinstance(image.get("extraInfo"), dict) else {"isH": "false", "isT": "false", "raw": "false"},
            "heightSize": int(image.get("heightSize") or 0),
            "imgPath": image.get("imgPath") or image.get("url"),
            "isQrCode": bool(image.get("isQrCode")),
            "labels": image.get("labels") if isinstance(image.get("labels"), list) else [],
            "major": bool(image.get("major")),
            "templateIndex": _text(image.get("templateIndex")) or "0",
            "thumbnail": image.get("thumbnail") or image.get("url"),
            "type": int(image.get("type") or 0),
            "url": image.get("url"),
            "widthSize": int(image.get("widthSize") or 0),
            "status": _text(image.get("status")) or "done",
        })

    return {
        "aiHostUsed": False,
        "aigc": "",
        "aigcRequestId": "",
        "asyncSecurityInfo": {"securityStrategyHitResult": {"FORBIDDEN": [], "WARN": []}},
        "baseParams": {
            "bizcode": _text(values.get("bizcode")),
            "bucketId": _text(values.get("bucketId")),
            "scene": _text(values.get("scene")),
            "simpleItem": _text(values.get("simpleItem")) or "true",
        },
        "bizLine": _text(values.get("attribute_biz_line")),
        "bizRent": "",
        "bizcode": _text(values.get("bizcode")),
        "bucketId": _text(values.get("bucketId")),
        "defaultPrice": bool(values.get("defaultPrice")),
        "editorVersion": "",
        "errorTipsMsg": values.get("errorTipsMsg"),
        "freebies": bool(values.get("freebies")),
        "imageInfoDOList": normalized_images,
        # 官方 xP 对新建草稿固定传空值；innerPublishType 只属于二维码深链。
        "innerPublishType": "",
        "itemAddrDTO": {
            "area": _text(address.get("area")),
            "city": _text(address.get("city")),
            "divisionId": _text(address.get("divisionId")),
            "gps": _text(address.get("gps")),
            "poiId": _text(address.get("poiId")),
            "poiName": _text(address.get("poiName")),
            "prov": _text(address.get("prov")),
        },
        "itemCatDTO": values.get("itemCatDTO") if isinstance(values.get("itemCatDTO"), dict) else {},
        "itemGroupDTO": {"groupId": ""},
        "itemLabelExtList": labels,
        "itemPostFeeDTO": {
            "canFreeShipping": bool(post_fee.get("canFreeShipping")),
            "supportFreight": bool(post_fee.get("supportFreight")),
            "onlyTakeSelf": bool(post_fee.get("onlyTakeSelf")),
            "postPriceInCent": _text(post_fee.get("postPriceInCent")) or None,
            "templateId": post_fee.get("templateId"),
        },
        "itemPriceDTO": {
            "origPriceInCent": _text(price.get("origPriceInCent")) or None,
            "priceInCent": _text(price.get("priceInCent")) or None,
        },
        "itemStatus": _text(values.get("itemStatus")) or "0",
        "itemTextDTO": {
            "desc": _text(text.get("desc")),
            "richTextDesc": text.get("richTextDesc"),
            "title": _text(text.get("title")) or _text(text.get("desc")),
            "titleDescSeparate": bool(text.get("titleDescSeparate")),
            "trackParamsRichTextStyle": text.get("trackParamsRichTextStyle"),
        },
        "itemTopicParams": {"topicInfos": []},
        "itemTypeStr": _text(values.get("itemTypeStr")) or "b",
        "publishScene": _text(values.get("publishScene")),
        "quantity": _text(values.get("quantity")) or "1",
        "scene": _text(values.get("scene")),
        "simpleItem": _text(values.get("simpleItem")) or "true",
        "sourceId": _text(values.get("sourceId")),
        "topics": values.get("topics") if isinstance(values.get("topics"), list) else [],
        "uniqueCode": _text(values.get("uniqueCode")),
        "userRightsProtocols": [
            {"enable": bool(protocol.get("enable")), "serviceCode": _text(protocol.get("serviceCode"))}
            for protocol in protocols
            if isinstance(protocol, dict) and _text(protocol.get("serviceCode"))
        ],
        "yhbItemInfoDTO": {
            "idleAppraiseScene": "",
            "settingsPreferences": {"assumeRule": "", "tradeRule": ""},
            "useYhbService": False,
        },
        "itemSkuList": item_skus,
        "itemProperties": item_properties,
    }


async def _service_publish_draft(
    *,
    payload: dict[str, Any],
    card_list: list[dict[str, Any]],
    cookie: str,
    platform_account_id: str,
    proxy: str | None,
) -> dict[str, Any]:
    """按闲鱼网页发布页的真实服务分类流程生成 APP 草稿。"""
    if not card_list:
        raise GoofishPublishError("服务类分类数据已失效，请重新选择商品分类")

    # 服务卡接口接收的是网页 xP 标准化后的 itemInfoJson；二维码里的
    # sourceId/draftbox 不属于草稿请求本身，不能混入 itemInfoJson。
    item_info = _draft_payload({
        **payload,
        "bizcode": "",
        "scene": "",
        "sourceId": "",
        "publishScene": "mainPublish",
    })
    item_info["publishScene"] = "mainPublish"
    official_cards = [{"cardData": card} for card in card_list]
    cards_response = await mtop_call(
        api=SERVICE_CARDS_API,
        data={
            "cpvList": json.dumps(official_cards, ensure_ascii=False, separators=(",", ":")),
            "itemInfoJson": json.dumps(item_info, ensure_ascii=False, separators=(",", ":")),
            "param": json.dumps(
                {"multiSkuEditingMode": "false", "settingsPreferences": None, "supportDefaultOpen": True},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
        cookies_str=cookie,
        account_id=platform_account_id,
        proxy=proxy or settings.goofish_proxy or None,
        origin="https://seller.goofish.com",
        referer="https://seller.goofish.com/",
    )
    if not cards_response.get("success"):
        return {
            "success": False,
            "message": f"闲鱼服务配置获取失败：{cards_response.get('error') or '未知错误'}",
            "account_invalid": bool(cards_response.get("account_invalid")),
            "cookies_str": cards_response.get("cookies_str") or cookie,
        }

    service_data = ((cards_response.get("res") or {}).get("data") or {})
    services = service_data.get("services") if isinstance(service_data, dict) else []
    if not isinstance(services, list):
        services = []
    payload["userRightsProtocols"] = [
        {"enable": bool(service.get("defaultEnable")), "serviceCode": _text(service.get("code"))}
        for service in services
        if isinstance(service, dict) and _text(service.get("code"))
    ]
    draft_payload = _draft_payload({
        **payload,
        "bizcode": "",
        "scene": "",
        "sourceId": "",
        "publishScene": "mainPublish",
    })
    draft_payload["uniqueCode"] = f"{int(time.time() * 1000)}{platform_account_id[-4:]}"
    draft_response = await mtop_call(
        api=DRAFT_PUBLISH_API,
        data=draft_payload,
        cookies_str=cards_response.get("cookies_str") or cookie,
        account_id=platform_account_id,
        proxy=proxy or settings.goofish_proxy or None,
        origin="https://seller.goofish.com",
        referer="https://seller.goofish.com/publish",
    )
    if not draft_response.get("success"):
        return {
            "success": False,
            "message": f"闲鱼服务类草稿创建失败：{draft_response.get('error') or '未知错误'}",
            "account_invalid": bool(draft_response.get("account_invalid")),
            "cookies_str": draft_response.get("cookies_str") or cards_response.get("cookies_str") or cookie,
        }
    draft_id = _draft_reference((draft_response.get("res") or {}).get("data"))
    if not draft_id:
        return {
            "success": False,
            "message": "闲鱼服务类草稿创建成功但未返回草稿编号，请稍后在闲鱼草稿箱查看",
            "account_invalid": False,
            "cookies_str": draft_response.get("cookies_str") or cookie,
        }
    return {
        "success": True,
        "requires_app": True,
        "draft_id": draft_id,
        "qr_content": f"fleamarket://simple_post?draftId={draft_id}&publishScene=mainPublish&sourceId=draftbox&innerPublishType=innerMainPublish",
        "message": "服务类商品草稿已生成，请扫码在闲鱼APP完成服务信息和库存发布",
        "account_invalid": False,
        "cookies_str": draft_response.get("cookies_str") or cookie,
        "item_id": None,
        "item_url": None,
    }


async def detect_publish_capability(*, cookie: str, platform_account_id: str, proxy: str | None = None) -> dict[str, Any]:
    response = await mtop_call(
        api=PREGET_API,
        data={},
        cookies_str=cookie,
        account_id=platform_account_id,
        proxy=proxy or settings.goofish_proxy or None,
        origin="https://www.goofish.com",
        referer="https://www.goofish.com/publish",
        extra_params={"spm_cnt": "a21ybx.publish.0.0"},
    )
    latest_cookie = response.get("cookies_str") or cookie
    if not response.get("success"):
        return {"success": False, "message": f"账号发布能力检测失败：{response.get('error') or '闲鱼接口调用失败'}", "account_invalid": bool(response.get("account_invalid")), "cookies_str": latest_cookie}
    raw = ((response.get("res") or {}).get("data") or {})
    commission = raw.get("commissionConfig") if isinstance(raw, dict) else {}
    commission = commission if isinstance(commission, dict) else {}
    tip_url = _text(commission.get("tipUrl"))
    default_title = _text(commission.get("defaultCommissionTitle"))
    commission_title = _text(commission.get("commissionTitle"))
    support_sku = raw.get("supportSkuOrInventory") if isinstance(raw, dict) else None
    fish_shop = (
        True if "fish-shop-service-fee-rule" in tip_url or "鱼小铺" in default_title or "鱼小铺" in commission_title
        else False if "fish-seller-service-fee-rule" in tip_url or default_title == "基础软件服务费"
        else bool(support_sku) if isinstance(support_sku, bool) else None
    )
    if fish_shop is None:
        return {"success": False, "message": "账号发布能力检测失败：闲鱼未返回可识别的账号类型", "account_invalid": False, "cookies_str": latest_cookie}
    return {
        "success": True,
        "message": "账号发布能力检测成功",
        "account_invalid": False,
        "cookies_str": latest_cookie,
        "is_fish_shop": fish_shop,
        "support_sku_or_inventory": bool(support_sku) if isinstance(support_sku, bool) else fish_shop,
        "commission_config": {
            "title": commission_title,
            "default_title": default_title,
            "tips": _text(commission.get("commissionTips")),
            "percent": _text(commission.get("percent")),
            "max_commission": _text(commission.get("maxCommission")),
            "tip_url": tip_url,
        },
    }


async def publish_item(*, item_data: dict[str, Any], cookie: str, platform_account_id: str, proxy: str | None, is_fish_shop: bool) -> dict[str, Any]:
    title = _text(item_data.get("title")); description = _text(item_data.get("description"))
    if not title or not description:
        raise GoofishPublishError("商品标题和商品描述不能为空")
    images = [_text(value) for value in item_data.get("images") or [] if _text(value)]
    if not images:
        raise GoofishPublishError("请至少上传一张商品图片")
    if len(images) > 9:
        raise GoofishPublishError("最多上传9张商品图片")
    if item_data.get("videos"):
        raise GoofishPublishError("视频发布需要先完成视频媒体上传配置，当前未提交视频内容")
    service_category = _is_service_category(item_data)
    if not is_fish_shop and not service_category and (item_data.get("specifications") or item_data.get("sku_rows")):
        raise GoofishPublishError("当前账号未开通鱼小铺，不能发布多规格和独立库存商品")
    personal = not is_fish_shop
    properties, sku_rows, has_sku = _sku_payload(item_data) if is_fish_shop else ([], [], False)
    requested_quantity = _quantity(item_data.get("quantity") or item_data.get("stock"))
    if personal and not service_category and requested_quantity != 1:
        raise GoofishPublishError("当前普通卖家账号不支持多库存商品，请将线上库存设为1或开通鱼小铺")
    labels = _category_labels(item_data)
    address_payload = await _resolve_item_address(item_data)
    uploaded_images = []
    for index, image in enumerate(images, 1):
        item = await _upload_image(image, cookie)
        item["major"] = index == 1
        uploaded_images.append(item)
    payload: dict[str, Any] = {
        "freebies": False,
        "itemTypeStr": "b",
        # 普通实物卖家只能发布单库存商品；服务类商品沿用 APP 服务草稿，
        # 因此必须保留用户填写的库存（例如手机端的 9999）。
        "quantity": str(requested_quantity) if service_category or (not personal and not has_sku) else "1",
        "simpleItem": "true",
        "imageInfoDOList": uploaded_images,
        "itemTextDTO": {"desc": description, "title": title, "titleDescSeparate": False},
        "itemLabelExtList": labels,
        "userRightsProtocols": [],
        "itemPostFeeDTO": _post_fee(item_data, personal=personal),
        "itemAddrDTO": address_payload,
        "defaultPrice": False,
        "itemCatDTO": {
            "catId": _text(item_data.get("platform_category_id")) or _text(item_data.get("platform_tb_category_id")),
            "catName": _text(item_data.get("platform_category_name")),
            "channelCatId": _text(item_data.get("platform_channel_category_id")),
            "leafId": _text(item_data.get("platform_leaf_id")),
            "tbCatId": _text(item_data.get("platform_tb_category_id")) or None,
        },
        "uniqueCode": f"{int(time.time() * 1000)}{platform_account_id[-4:]}",
        "sourceId": "pcMainPublish" if personal else "pcBackendPublish",
        "bizcode": "pcMainPublish" if personal else "pcMainPublish",
        "publishScene": "pcMainPublish" if personal else "pcBackendPublish",
    }
    if personal:
        payload["itemPriceDTO"] = {"origPriceInCent": _price_cent(item_data.get("original_price") or item_data.get("price"), "原价"), "priceInCent": _price_cent(item_data.get("price"), "售价")}
    else:
        payload["itemPriceDTO"] = {} if has_sku else {"origPriceInCent": _price_cent(item_data.get("original_price") or item_data.get("price"), "原价"), "priceInCent": _price_cent(item_data.get("price"), "售价")}
        if has_sku:
            payload["itemProperties"] = properties; payload["itemSkuList"] = sku_rows
    if service_category:
        return await _service_publish_draft(
            payload=payload,
            card_list=[item for item in (item_data.get("platform_card_list") or []) if isinstance(item, dict)],
            cookie=cookie,
            platform_account_id=platform_account_id,
            proxy=proxy,
        )
    response = await mtop_call(
        api=PERSONAL_PUBLISH_API if personal else FISH_SHOP_PUBLISH_API,
        data={"inputJson": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))} if not personal else payload,
        cookies_str=cookie,
        account_id=platform_account_id,
        proxy=proxy or settings.goofish_proxy or None,
        origin="https://www.goofish.com",
        referer="https://www.goofish.com/publish",
        extra_params={"spm_cnt": "a21ybx.publish.0.0"},
        extra_headers={"idle_site_biz_code": "COMMONPRO"} if not personal else None,
    )
    if not response.get("success"):
        return {"success": False, "message": f"闲鱼接口发布失败：{response.get('error') or '未知错误'}", "account_invalid": bool(response.get("account_invalid")), "cookies_str": response.get("cookies_str") or cookie, "item_id": None, "item_url": None}
    item_id, item_url = _item_reference((response.get("res") or {}).get("data"))
    if item_id:
        item_url = f"https://www.goofish.com/item?id={item_id}"
    return {"success": True, "message": "商品发布成功", "account_invalid": False, "cookies_str": response.get("cookies_str") or cookie, "item_id": item_id, "item_url": item_url}
