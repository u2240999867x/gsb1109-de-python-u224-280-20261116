from LnkParse3.target.lnk_target_base import LnkTargetBase


class Unknown(LnkTargetBase):
    def __init__(self, *args, **kwargs):
        self.name = "Unknown"
        return super().__init__(*args, **kwargs)

    def extension_start(self):
        # The payload layout is unknown, so no byte range may be claimed as
        # an extension. The whole item remains in ``raw`` for later review.
        return None

    def as_item(self):
        item = super().as_item()
        item["size"] = self.SIZE_OF_TARGET_SIZE + self.size()
        return item
