from setuptools import setup, find_packages

setup(
    name="charisg-backend-shared",
    version="1.0.0",
    description="Charis G Platform 공용 백엔드 모듈 (Hub/DS/PA 공용)",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "requests>=2.31.0",
        "httpx>=0.27.0",
        "python-dotenv>=1.0.0",
    ],
    extras_require={
        "ai": ["google-generativeai>=0.7.0", "anthropic>=0.30.0"],
        "sheets": ["gspread>=6.0.0", "google-auth>=2.30.0"],
        "image": ["Pillow>=10.0.0"],
        "shopify": ["ShopifyAPI>=12.5.0"],
    },
)
