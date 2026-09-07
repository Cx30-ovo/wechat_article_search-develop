import importlib
import sys
import types
import unittest


if "pymongo" not in sys.modules:
    try:
        import pymongo  # noqa: F401
    except ImportError:
        module = types.ModuleType("pymongo")
        module.ASCENDING = 1
        module.MongoClient = object
        sys.modules["pymongo"] = module


api = importlib.import_module("services.control_api.app")


class FakeAccounts:
    def find_one(self, query, projection):
        return {"id": "ACCOUNT_XIAMEN"}


class FakeArticles:
    def __init__(self):
        self.update = None

    def find_one(self, query, projection):
        if projection == {"_id": 1} and "article.urlNormalized" in query:
            return {"_id": "article-1"}
        return None

    def update_one(self, selector, update, upsert):
        self.update = (selector, update, upsert)
        return types.SimpleNamespace(upserted_id="article-1")


class ControlApiTests(unittest.TestCase):
    def test_normalize_article_url_matches_desktop_rule(self):
        value = api.normalize_article_url(
            "HTTPS://MP.WEIXIN.QQ.COM/s/example?z=2&a=1#fragment"
        )
        self.assertEqual(value, "https://mp.weixin.qq.com/s/example?a=1&z=2")

    def test_ingest_uses_existing_nested_article_schema(self):
        repository = api.Repository.__new__(api.Repository)
        repository.accounts = FakeAccounts()
        repository.articles = FakeArticles()

        article_id, created = repository.ingest_article(
            {
                "url": "https://mp.weixin.qq.com/s/example?b=2&a=1",
                "account_name": "厦门日报",
                "title": "测试标题",
                "content": "测试正文",
                "publish_time": "2026-08-31 10:30",
                "interaction": {"shareCount": 3},
            }
        )

        self.assertEqual(article_id, "article-1")
        self.assertTrue(created)
        selector, update, upsert = repository.articles.update
        self.assertTrue(upsert)
        self.assertIn("article.urlNormalized", selector)
        self.assertEqual(update["$setOnInsert"]["account.name"], "厦门日报")
        self.assertEqual(update["$setOnInsert"]["article.content.text"], "测试正文")
        self.assertEqual(update["$push"]["interactionHistory"]["$each"][0]["shareCount"], 3)


if __name__ == "__main__":
    unittest.main()
