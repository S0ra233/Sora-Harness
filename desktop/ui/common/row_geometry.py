"""共享的行高索引；只保存数字，不创建离屏控件或参与业务状态。"""


class RowGeometry:
    def __init__(self, spacing=28, *, estimate):
        self.spacing = spacing
        self._estimate = estimate
        self.keys = ()
        self.positions = {}
        self.heights = []
        self._tree = [0]
        self.width = 0

    def reset(self, rows, width):
        old = {key: self.heights[index] for key, index in self.positions.items()} if width == self.width else {}
        self.keys = tuple(rows)
        self.positions = {key: index for index, key in enumerate(self.keys)}
        self.width = width
        self.heights = [old[key] if key in old else self._estimate(rows[key], width) for key in self.keys]
        self._tree = [0] + [height + self.spacing for height in self.heights]
        for index in range(1, len(self._tree)):
            parent = index + (index & -index)
            if parent < len(self._tree):
                self._tree[parent] += self._tree[index]

    def offset_at(self, index):
        total = 0
        while index:
            total += self._tree[index]
            index -= index & -index
        return total

    def offset(self, key):
        return self.offset_at(self.positions[key])

    @property
    def total(self):
        return max(0, self.offset_at(len(self.keys)) - self.spacing)

    def locate(self, y):
        """返回坐标所在行；空行集返回零，由调用者判断是否存在内容。"""
        index, total = 0, 0
        bit = 1 << (len(self.keys).bit_length() - 1) if self.keys else 0
        while bit:
            target = index + bit
            if target < len(self._tree) and total + self._tree[target] <= y:
                total += self._tree[target]
                index = target
            bit >>= 1
        return min(index, max(0, len(self.keys) - 1))

    def update(self, key, height):
        index = self.positions[key]
        height = max(1, round(height))
        delta = height - self.heights[index]
        if not delta:
            return False
        self.heights[index] = height
        index += 1
        while index < len(self._tree):
            self._tree[index] += delta
            index += index & -index
        return True
