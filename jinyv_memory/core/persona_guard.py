# -*- coding: utf-8 -*-
"""人设宪法校验（阶段二）：硬规则立即处置，软漂移只统计（决策 3）

硬规则（命中即违规，由中间件带纠正提示重生成一次）：
  - 禁忌词
  - 禁用标点
  - 风格串味：古风角色出现口语词 / 口语角色出现文言词

软漂移（只计数不干预，避免单轮情绪起伏被误判）：
  - 平均句长相对基线偏离 ±50%
  - 句末语气词密度高于基线 2 倍
"""
import re

COLLOQUIAL = ("emm", "ok", "OK", "笑死", "哇塞", "嗯呐", "好哒", "233", "么么", "绝了")
CLASSICAL = ("在下", "阁下", "鄙人", "承蒙", "窃以为", "此乃", "故而", "倘若", "兄台", "大人", "启禀")
PARTICLES = ("啊", "呀", "呢", "嘛", "哦", "啦", "哎", "哟", "呐", "罢了")
WINDOW = 20          # 基线滑动窗口（轮）


def _sentences(reply):
    return [s.strip() for s in re.split(r"[。！？…\n]+", reply or "") if s.strip()]


class PersonaGuard:
    def __init__(self, persona):
        self.taboo = tuple(w for w in (persona.get("taboo_words") or []) if w)
        self.forbidden_punct = tuple(p for p in (persona.get("forbidden_punct") or []) if p)
        self.style = persona.get("style")            # classical | colloquial | None
        self.hard_hits = 0
        self.soft_len_hits = 0
        self.soft_particle_hits = 0
        self._len_window = []
        self._particle_window = []

    def hard_violations(self, reply):
        v = []
        for w in self.taboo:
            if w in reply:
                v.append(f"taboo:{w}")
        for p in self.forbidden_punct:
            if p in reply:
                v.append(f"punct:{p}")
        if self.style == "classical":
            for w in COLLOQUIAL:
                if w in reply:
                    v.append(f"style:colloquial_word:{w}")
        elif self.style == "colloquial":
            for w in CLASSICAL:
                if w in reply:
                    v.append(f"style:classical_word:{w}")
        return v

    def observe(self, reply):
        """软漂移统计：先记录本轮，再用「除本轮之外」的基线做比较。"""
        sents = _sentences(reply)
        if not sents:
            return
        cur_len = sum(len(s) for s in sents) / len(sents)
        if len(self._len_window) >= 5:
            base = sum(self._len_window) / len(self._len_window)
            if base and (cur_len > base * 1.5 or cur_len < base * 0.5):
                self.soft_len_hits += 1
        self._len_window = (self._len_window + [cur_len])[-WINDOW:]

        chars = max(1, len(reply))
        density = 100.0 * sum(reply.count(p) for p in PARTICLES) / chars
        if len(self._particle_window) >= 5:
            base = sum(self._particle_window) / len(self._particle_window)
            if base and density > base * 2:
                self.soft_particle_hits += 1
        self._particle_window = (self._particle_window + [density])[-WINDOW:]

    def check(self, reply):
        """返回硬违规列表，并顺带更新软漂移统计。"""
        hard = self.hard_violations(reply)
        if hard:
            self.hard_hits += 1
        self.observe(reply)
        return hard

    def stats(self):
        return {"hard": self.hard_hits, "soft_len": self.soft_len_hits,
                "soft_particle": self.soft_particle_hits}
