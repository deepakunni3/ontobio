# DOCS
# See https://github.com/pypa/virtualenv/issues/149
pydoc-%:
	python -m pydoc ontobio.ontol

#PACKAGES = prefixcommons scigraph biogolr
PACKAGES = ontobio prefixcommons
subpackage_tests: $(patsubst %,test-%,$(PACKAGES))

test:
	pytest tests/*.py

# only run local tests
travis_test:
	pytest tests/test_*local*.py tests/test_*parser*.py

cleandist:
	rm dist/*

# TODO: manually increment version, run . bump.sh, then this
release: cleandist
	python setup.py sdist bdist_wheel bdist_egg
	twine upload dist/*

nb:
	PYTHONPATH=.. jupyter notebook

foo:
	./bin/flask2marshmallow.pl ../biolink-api/biolink/datamodel/serializers.py 