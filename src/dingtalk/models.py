from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field


@dataclass
class DingTalkMessage:
    msgtype: str
    chatid: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        raise NotImplementedError


@dataclass
class TextMessage(DingTalkMessage):
    content: str = ""
    at_mobiles: List[str] = field(default_factory=list)
    is_at_all: bool = False

    def __post_init__(self):
        self.msgtype = "text"

    def to_dict(self) -> Dict[str, Any]:
        at_dict: Dict[str, Any] = {}
        if self.at_mobiles:
            at_dict["atMobiles"] = self.at_mobiles
        if self.is_at_all:
            at_dict["isAtAll"] = True
        
        data: Dict[str, Any] = {
            "msgtype": self.msgtype,
            "text": {"content": self.content}
        }
        if at_dict:
            data["at"] = at_dict
        return data


@dataclass
class MarkdownMessage(DingTalkMessage):
    title: str = ""
    text: str = ""
    at_mobiles: List[str] = field(default_factory=list)
    is_at_all: bool = False

    def __post_init__(self):
        self.msgtype = "markdown"

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "msgtype": self.msgtype,
            "markdown": {
                "title": self.title,
                "text": self.text
            }
        }
        at_dict: Dict[str, Any] = {}
        if self.at_mobiles:
            at_dict["atMobiles"] = self.at_mobiles
        if self.is_at_all:
            at_dict["isAtAll"] = True
        
        data: Dict[str, Any] = {
            "msgtype": self.msgtype,
            "markdown": {"title": self.title, "text": self.text}
        }
        if at_dict:
            data["at"] = at_dict
        return data


@dataclass
class LinkMessage(DingTalkMessage):
    title: str = ""
    text: str = ""
    message_url: str = ""
    pic_url: str = ""

    def __post_init__(self):
        self.msgtype = "link"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "msgtype": self.msgtype,
            "link": {
                "title": self.title,
                "text": self.text,
                "messageUrl": self.message_url,
                "picUrl": self.pic_url
            }
        }


@dataclass
class ActionCardButton:
    title: str
    url: str


@dataclass
class ActionCardMessage(DingTalkMessage):
    title: str = ""
    text: str = ""
    btn_orientation: str = "0"
    btns: List[ActionCardButton] = field(default_factory=list)
    single_title: str = ""
    single_url: str = ""

    def __post_init__(self):
        self.msgtype = "actionCard"

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "msgtype": self.msgtype,
            "actionCard": {
                "title": self.title,
                "text": self.text,
                "btnOrientation": self.btn_orientation
            }
        }
        
        if self.single_title and self.single_url:
            data["actionCard"]["singleTitle"] = self.single_title
            data["actionCard"]["singleURL"] = self.single_url
        elif self.btns:
            data["actionCard"]["buttons"] = [
                {"title": btn.title, "actionURL": btn.url}
                for btn in self.btns
            ]
        
        return data


@dataclass
class CallbackMessage:
    msgtype: str
    create_at: int
    sender: str
    sender_nick: str
    chatid: str
    is_admin: bool
    text: Optional[Dict[str, str]] = None
    markdown: Optional[Dict[str, str]] = None
    robot_code: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CallbackMessage":
        text_data = data.get("text")
        markdown_data = data.get("markdown")
        
        return cls(
            msgtype=data.get("msgtype", ""),
            create_at=data.get("createAt", 0),
            sender=data.get("sender", ""),
            sender_nick=data.get("senderNick", ""),
            chatid=data.get("chatid", ""),
            is_admin=data.get("isAdmin", False),
            robot_code=data.get("robotCode"),
            text=text_data,
            markdown=markdown_data
        )

    def get_content(self) -> str:
        if self.msgtype == "text" and self.text:
            return self.text.get("content", "")
        if self.msgtype == "markdown" and self.markdown:
            return self.markdown.get("text", "")
        return ""


@dataclass
class SendResult:
    success: bool
    errcode: int = 0
    errmsg: str = ""
    task_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SendResult":
        return cls(
            success=data.get("errcode", 0) == 0,
            errcode=data.get("errcode", 0),
            errmsg=data.get("errmsg", ""),
            task_id=data.get("taskId")
        )
