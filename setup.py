from setuptools import setup, find_packages

setup(
    name="tmux-agent-hibernator",
    version="0.2.0",
    description="Automatically hibernate and restore idle AI agent sessions in tmux",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    author="Andy Mack",
    url="https://github.com/amack87/tmux-agent-hibernator",
    license="MIT",
    packages=find_packages(),
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "tmux-agent-hibernator=cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
