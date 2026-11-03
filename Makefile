WHL_BUILD_DIR :=package
# DOC_BUILD_DIR :=docs/build/

# default rule
default: whl docs

# .PHONY: docs
# docs:
# 	bash .dev_scripts/build_docs.sh

.PHONY: docs
docs:
	pip install -r requirements/docs.txt
	$(MAKE) docs-en
	$(MAKE) docs-zh

.PHONY: docs-en
docs-en:
	cd docs/en && make clean && make html

.PHONY: docs-zh
docs-zh:
	cd docs/zh && make clean && make html

.PHONY: lint
lint:
	ruff check .
	ruff format --check .

.PHONY: format
format:
	ruff check --fix .
	ruff format .

.PHONY: whl
whl:
	python setup.py sdist bdist_wheel

.PHONY: install
install:
	pip install -e .
