from __future__ import annotations

import re


def terms(text: str) -> list[str]:
    """把查询切成可比的关键词：英文按词，中文按两个字一组。

    中文不分词的话，「受体文件」整句只能整句匹配，换个语序就查不到；
    两个字一组不引入词典，代价是极短查询切不出来，所以两个字以内按整词留。
    记忆检索与历史检索共用这一份切法：两边对同一句话切出不同的词，
    「同一个问题在两处命中不同条目」就无从解释。
    """
    found: list[str] = []
    for token in re.split(r"[\s,，。;；:：/()（）]+", text):
        if not token:
            continue
        found.extend(re.findall(r"[a-z0-9_.\-]+", token))
        cjk = "".join(char for char in token if not char.isascii())
        if len(cjk) <= 2:
            if cjk:
                found.append(cjk)
        else:
            found.extend(cjk[index : index + 2] for index in range(len(cjk) - 1))
    return list(dict.fromkeys(found))
