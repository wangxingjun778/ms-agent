<!---
Copyright (c) Alibaba, Inc. and its affiliates.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Contribute to MS-Agent

We sincerely appreciate every contribution, regardless of its form. While contributing code is certainly impactful,
there are many other ways to aid the community.
Providing answers to questions, extending a helping hand to peers, and enhancing the documentation are just as essential
to our collective success.


Your support doesn't go unnoticed when you share your experiences.
Mention our library in your blog posts about the phenomenal projects you've built with its help, give us a shoutout on
Twitter or WeChat, whenever our library comes to your rescue,
or simply give the repository a star as a token of your appreciation.

However, you choose to contribute, please be mindful and respect our
[code of conduct](https://github.com/modelscope/MS-Agent/blob/master/CODE_OF_CONDUCT.md).


## Ways to contribute

There are several ways you can contribute to MS-Agent:

* Fix outstanding issues with the existing code, found them at [here](https://github.com/modelscope/MS-Agent/issues).
* Submit issues related to bugs or desired new features [here](https://github.com/modelscope/MS-Agent/issues/new/choose)
* Implement new tools, please refer the doc [here](https://github.com/modelscope/MS-Agent/tree/master/docs/contributing/tool_contribution_guide.md)
* Contribute to the examples or to the documentation, [here](https://github.com/modelscope/MS-Agent/tree/master/examples)

> All contributions are equally valuable to the community.

## Styles

MS-Agent is formatted and linted with [ruff](https://docs.astral.sh/ruff/).
The rules live in [ruff.toml](https://github.com/modelscope/MS-Agent/blob/master/ruff.toml) and the
WebUI is checked by its own workflow. No git hooks are installed, so run the checks yourself before
committing:

```shell
pip install ruff==0.15.11

# check the whole repository (same commands as CI)
make lint

# apply import sorting and formatting fixes
make format
```
