.PHONY: test compile deb clean run

compile:
	python3 -m py_compile vessel_keeper.py jurisdictions/*.py

test: compile
	cd /tmp/opencode && python3 test_core.py && DISPLAY=:0 python3 test_gtk.py && DISPLAY=:0 python3 test_boot.py && DISPLAY=:0 python3 test_exit_final.py

deb:
	bash packaging/build_deb.sh

run:
	python3 vessel_keeper.py

clean:
	find . -name '__pycache__' -type d -empty -delete
	find . -name '*.pyc' -delete
	rm -rf build dist *.egg-info
