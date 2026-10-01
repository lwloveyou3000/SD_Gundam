"""Table separators for the native Tk tree widget."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class GridTreeview(ttk.Treeview):
    """Keep native selection and scrolling, with visible cell boundaries."""

    def __init__(self, master, *, rowheight: int, **kwargs):
        super().__init__(master, **kwargs)
        self._rowheight = rowheight
        self._grid_job = None
        self._grid_closed = False
        self._grid_lines: list[tk.Frame] = []
        self._line_rects: list[tuple[int, int, int, int]] = []
        self._scrollbar = None
        for sequence in ("<Configure>", "<ButtonRelease-1>", "<B1-Motion>"):
            self.bind(sequence, lambda _event: self.refresh_grid(), add="+")
        self.bind("<Destroy>", self._dispose_grid, add="+")

    def set_scrollbar(self, scrollbar) -> None:
        self._scrollbar = scrollbar
        self.configure(yscrollcommand=self._scrolled)

    def _scrolled(self, first, last) -> None:
        if self._scrollbar is not None:
            self._scrollbar.set(first, last)
        self.refresh_grid()

    def refresh_grid(self) -> None:
        if not self._grid_closed and self._grid_job is None:
            self._grid_job = self.after_idle(self._draw_grid)

    def cancel_grid(self) -> None:
        if self._grid_job is not None:
            self.after_cancel(self._grid_job)
            self._grid_job = None

    def destroy(self) -> None:
        self._grid_closed = True
        self.cancel_grid()
        super().destroy()

    def _relay_input(self, event, sequence):
        options = {"x": event.x_root - self.winfo_rootx(),
                   "y": event.y_root - self.winfo_rooty()}
        if sequence == "<MouseWheel>":
            options["delta"] = event.delta
        self.event_generate(sequence, **options)
        return "break"

    def _make_line(self) -> tk.Frame:
        line = tk.Frame(self, background="#cbd3dd", borderwidth=0, highlightthickness=0)
        for sequence in ("<Button-1>", "<ButtonRelease-1>", "<MouseWheel>"):
            line.bind(sequence, lambda event, seq=sequence: self._relay_input(event, seq))
        return line

    def _draw_grid(self) -> None:
        self._grid_job = None
        width, height = self.winfo_width(), self.winfo_height()
        if width < 3 or height < 3:
            return
        # The native heading may span multiple text lines; query its actual
        # region instead of assuming a font-dependent header height.
        heading = [y for y in range(1, min(height, 64))
                   if self.identify_region(10, y) in {"heading", "separator"}]
        top = max(heading, default=1) + 1
        rects = [(1, top, width - 2, 1)]
        x = 2
        for column in ("#0", *self["columns"]):
            x += self.column(column, "width")
            if x < width - 1:
                rects.append((x, top, 1, max(1, height - top - 1)))
        row_top = top
        for item in self.get_children():
            bounds = self.bbox(item)
            if bounds:
                row_top = bounds[1]
                break
        y = row_top + self._rowheight - 1
        while y < top:
            y += self._rowheight
        while y < height - 1:
            rects.append((1, y, width - 2, 1))
            y += self._rowheight
        self._line_rects = rects
        while len(self._grid_lines) < len(rects):
            self._grid_lines.append(self._make_line())
        for line, (x, y, w, h) in zip(self._grid_lines, rects):
            line.place(x=x, y=y, width=w, height=h)
            line.lift()
        for line in self._grid_lines[len(rects):]:
            line.place_forget()

    def _dispose_grid(self, event) -> None:
        if event.widget is self:
            self._grid_closed = True
            self.cancel_grid()
