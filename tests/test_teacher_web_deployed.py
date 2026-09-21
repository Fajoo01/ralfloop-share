"""Opt-in smoke of the installed loopback service, without model inference cost."""
import os
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("TEACHER_WEB_DEPLOYED") != "1", reason="Requires installed loopback service and explicit demo enrollment")
def test_installed_service_mobile_activity_material_audio_and_isolation():
    import requests
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    base="http://127.0.0.1:19139"
    assert requests.get(base+'/health',timeout=10).json() == {"status":"ok"}
    options=webdriver.ChromeOptions()
    for option in ('--headless=new','--no-sandbox','--disable-dev-shm-usage'): options.add_argument(option)
    browser=webdriver.Chrome(service=Service(os.environ.get('TEACHER_CHROMEDRIVER',str(Path.home()/'.cache/selenium/chromedriver/linux64/147.0.7727.117/chromedriver'))),options=options)
    wait=WebDriverWait(browser,15)
    try:
        browser.execute_cdp_cmd('Emulation.setDeviceMetricsOverride',{'width':390,'height':844,'deviceScaleFactor':1,'mobile':True})
        browser.get(base+'/login')
        wait.until(EC.presence_of_element_located((By.ID,'field-card'))).send_keys('DEMO-MIDDLE')
        browser.find_element(By.ID,'field-credential').send_keys('StudioDemo!2026')
        browser.find_element(By.XPATH,"//button[.='Entra']").click()
        wait.until(EC.text_to_be_present_in_element((By.ID,'main'),'Ciao, Marco Demo'))
        assert browser.execute_script('return document.documentElement.scrollWidth <= window.innerWidth')
        activity=browser.execute_async_script("""const done=arguments[0];fetch('/api/activities',{method:'POST',headers:{'Content-Type':'application/json','X-Teacher-Request':'1'},body:JSON.stringify({topic:'fractions',activity_type:'matching'})}).then(r=>r.json()).then(done);""")
        browser.get(base+'/activity?id='+activity['activity_id'])
        wait.until(EC.presence_of_element_located((By.ID,'pair-0')))
        for i,value in enumerate(['2/4','2/6','6/8']): Select(browser.find_element(By.ID,f'pair-{i}')).select_by_visible_text(value)
        browser.find_element(By.XPATH,"//button[.='Invia risposta']").click()
        wait.until(EC.text_to_be_present_in_element((By.ID,'main'),'Risposta corretta.'))
        assert 'XP' in browser.find_element(By.ID,'main').text
        browser.get(base+'/books')
        wait.until(EC.element_to_be_clickable((By.XPATH,"//button[.='Ascolta']"))).click()
        wait.until(EC.text_to_be_present_in_element((By.ID,'main'),'Riproduci'))
        assert 'Peppone' in browser.find_element(By.ID,'main').text
        assert 'lettura browser' in browser.find_element(By.ID,'main').text
        other=requests.Session()
        other.headers.update({'Origin':base,'X-Teacher-Request':'1'})
        assert other.post(base+'/api/login',json={'membership_card_id':'DEMO-PRIMARY','credential':'StudioDemo!2026'},timeout=10).status_code == 200
        assert other.get(base+'/api/activities/'+activity['activity_id'],timeout=10).status_code == 404
        assert other.get(base+'/api/progress',timeout=10).json()['xp'] == 0
    finally:
        browser.quit()
