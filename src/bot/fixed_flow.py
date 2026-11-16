"""明确 @Bot + 正文关键词的确定性路由，检索标题链接，日文源仅用模型翻译。"""
import json
import os
import re
from dataclasses import dataclass

from src.models.base import SenderType
from src.utils.identity import is_at_bot


@dataclass(frozen=True)
class FixedFlow:
    keywords: tuple[str, ...] = ()

    @classmethod
    def from_env(cls):
        try:
            values = json.loads(os.environ.get('FIXED_FLOW_KEYWORDS', '[]'))
        except (ValueError, TypeError):
            raise ValueError('FIXED_FLOW_KEYWORDS 必须是 JSON 字符串数组') from None
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError('FIXED_FLOW_KEYWORDS 必须是非空字符串组成的 JSON 数组')
        return cls(tuple(dict.fromkeys(v.strip() for v in values)))

    def match(self, message, *, addressed: bool = False):
        text = message.text or ''
        if message.sender_type != SenderType.OTHER or not (is_at_bot(text) or addressed):
            return None
        # 去掉 @昵称，避免昵称中的关键字被当成指令。引用内容不参与匹配。
        body = re.sub(r'@[^\s\u2005]+', '', text)
        return next((word for word in self.keywords if word in body), None)

    def reply(self, message, keyword: str):
        """返回命中关键词对应的页面标题与链接，不读取正文。"""
        from src.bot.archero_hub import ArcheroHub, HubError
        from src.bot.archero_wiki import ArcheroWiki, WikiError
        sections = [f'“{keyword}”相关攻略：']
        for name, source, error in (
            ('Archero2 Hub（中文）', ArcheroHub(), HubError),
            ('日文 Wiki（仅弓箭传说2，标题已译中文）', ArcheroWiki(), WikiError),
        ):
            try:
                pages = source.lookup(keyword)
            except error:
                sections.append(f'{name}：暂时读取失败，请稍后再试。')
                continue
            if not pages:
                if isinstance(source, ArcheroHub):
                    sections.append(f'{name}：未找到二代相关内容。')
                continue
            sections.append(name + '：\n' + '\n\n'.join(
                f"{page['title']}\n{page['url']}" for page in pages
            ))
        return '\n\n'.join(sections)
