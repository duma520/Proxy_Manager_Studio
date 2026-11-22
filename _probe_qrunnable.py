#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隔离测试：QRunnable + QThreadPool.globalInstance() + QObject 信号 是否能在主线程回包。"""
import sys, time
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, QRunnable, Signal, Slot, QThreadPool

app = QApplication.instance() or QApplication([])

class Sig(QObject):
    done = Signal(str)

class Task(QRunnable):
    def __init__(self, sig):
        super().__init__()
        self.s = sig
        self.setAutoDelete(True)
    @Slot()
    def run(self):
        print("  [worker] run() 执行了")
        self.s.done.emit("hello from worker")

got = []
def on_done(v):
    print("  [main] 收到信号:", v)
    got.append(v)

sig = Sig()
sig.done.connect(on_done)
t = Task(sig)
print("start 前 activeThreadCount =", QThreadPool.globalInstance().activeThreadCount())
QThreadPool.globalInstance().start(t)
print("start 后 activeThreadCount =", QThreadPool.globalInstance().activeThreadCount())
for i in range(30):
    app.processEvents()
    time.sleep(0.1)
    if i % 5 == 0:
        print(f"  t={i*0.1:.1f}s got={got} active={QThreadPool.globalInstance().activeThreadCount()}")
    if got:
        print(">>> 信号已回包")
        break
print("最终 got =", got)
sys.exit(0)
